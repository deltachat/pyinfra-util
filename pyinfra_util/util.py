import importlib.resources
import subprocess
from io import StringIO

from pyinfra import host, logger
from pyinfra.api import FactBase
from pyinfra.operations import apt, files, server


def get_pass(filename: str, fail_ok=False) -> str:
    """Get the data from the password manager."""
    try:
        print(f"       \033[1mInvoking pass for \033[0m{filename}")
        r = subprocess.run(["pass", "show", filename], capture_output=True)
        if not fail_ok:
            assert r.returncode == 0

    except FileNotFoundError:
        print("Please install pass and pull the latest version of our pass secrets")
        exit()
    except AssertionError:
        print(f"Pass failed: {r.stderr.decode('utf-8')}")
        exit(r.returncode)
    return r.stdout.decode("utf-8")


def create_unix_user(
    unix_user,
    unix_groups: [str] = [],
    enable_linger=True,
    ssh_keys=None,
    home=None,
    create_home=False,
    protect_home=False,
):
    """Create a UNIX user for the bot, add it to a group if provided.

    :param unix_user: the username for the UNIX user
    :param unix_groups: a list of groups if you want to add the bot to groups
    :param enable_linger: whether the user should be able to run systemd units in the background
    :param ssh_keys: a list of SSH keys which should be able to login to the user
    :param home: home directory, defaults to /home/{unix_user}
    :param create_home: populate a newly created home dir from /etc/skel (useradd -m)
    :param protect_home: whether to set g+s for the home directory
    """
    home = home or f"/home/{unix_user}"
    server.user(
        name=f"Add user {unix_user}",
        user=unix_user,
        groups=unix_groups,
        present=True,
        shell="/bin/bash",
        home=home,
        create_home=create_home,
        public_keys=ssh_keys,
    )

    if enable_linger:
        server.shell(
            name=f"enable {unix_user}'s systemd units to auto-start at boot",
            commands=[f"loginctl enable-linger {unix_user}"],
        )
        files.line(
            name=f"{unix_user} .profile: ensure XDG_RUNTIME_DIR",
            path=f"{home}/.profile",
            line='export XDG_RUNTIME_DIR="/run/user/$UID"',
            _su_user=unix_user,
            _use_su_login=True,
        )
        files.line(
            name=f"{unix_user} .profile: ensure DBUS_SESSION_BUS_ADDRESS",
            path=f"{home}/.profile",
            line='export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"',
            _su_user=unix_user,
            _use_su_login=True,
        )

    if protect_home:
        files.directory(
            name=f"chmod: Protect /home/{unix_user} from other users",
            path=f"/home/{unix_user}",
            present=True,
            user=unix_user,
            mode="770",
            recursive=False,
        )


class UserHome(FactBase):
    def command(self, user):
        return f"getent passwd {user} | cut -d: -f6"

    def process(self, output):
        return output[0] if output else None


def resolve_ssh_keys(*keys) -> list[str]:
    return [k if k.startswith("ssh-") else get_pass(k).strip() for k in keys]


def deploy_ssh_keys(user, *keys, home=None):
    """Set a user's authorized_keys from a mix of raw public keys and pass paths.

    :param home: the user's home directory; needs to be passed explicitly for fact collection.
    """
    home = home or host.get_fact(UserHome, user)
    server.user_authorized_keys(
        name=f"Set {user}'s SSH keys",
        user=user,
        public_keys=resolve_ssh_keys(*keys),
        authorized_key_directory=f"{home}/.ssh/",
        delete_keys=True,
    )


def deploy_srv_wrapper(unix_user: str, home=None):
    """Upload the srv.sh systemd user service wrapper to `unix_user`'s home."""
    home = home or host.get_fact(UserHome, unix_user)
    files.put(
        name=f"upload srv.sh to {unix_user}",
        src=importlib.resources.files(__package__).joinpath("scripts/srv.sh"),
        dest=f"{home}/srv.sh",
        user=unix_user,
        mode="0755",
    )


def parse_env(env: [str], var: str) -> str:
    """Parse an environment file for environment variables and return their value

    :param env: the environment file
    :param var: the environment variable name
    :return: the value of the variable in the env file
    """
    envlines = env.split("\n")
    for line in envlines:
        if var == line.split("=", 1)[0]:
            return line.split("=", 1)[1]


def _parse_data_csv(key: str) -> set | None:
    raw = host.data.get(key)
    return set(raw.split(",")) if raw else None


def run_deploys(deploys: dict[str, callable], only_key: str = "only", skip_key: str = "skip"):
    """
    Run deploy functions with optional filtering via host.data.

    Args:
        deploys: Dict mapping name -> @deploy-decorated function
        only_key: host.data key for whitelist (csv)
        skip_key: host.data key for blacklist (csv)

    Usage:
        pyinfra inv.py deploy.py               # all
        pyinfra inv.py deploy.py -d only=nginx # just nginx
        pyinfra inv.py deploy.py -d skip=nginx # all but nginx
    """
    only = _parse_data_csv(only_key)
    skip = _parse_data_csv(skip_key) or set()
    available = set(deploys.keys())

    for name in (only or set()) | skip:
        if name not in available:
            logger.warning(f"Unknown deploy target: '{name}' (available: {', '.join(sorted(available))})")
            return

    for name, fn in deploys.items():
        if (only is None or name in only) and name not in skip:
            fn()


class AptCodename(FactBase):
    command = ". /etc/os-release && echo $VERSION_CODENAME"


class AptArchitecture(FactBase):
    command = "dpkg --print-architecture"


def deploy_apt_source(name, repo_uri, key_uri, components="stable", suites=None):
    keyrings_path = "/etc/apt/keyrings"
    asc_path = f"{keyrings_path}/{name}.asc"
    gpg_path = f"{keyrings_path}/{name}.gpg"
    sources_path = f"/etc/apt/sources.list.d/{name}.sources"
    arch = host.get_fact(AptArchitecture)
    # Repos using the flat layout (e.g. apt.grafana.com) need a fixed suite like
    # "stable"; distro mirrors default to the running release's codename.
    suites = suites or host.get_fact(AptCodename)

    files.directory(
        path=keyrings_path,
        mode="755",
    )

    asc = files.download(name=f"{name}: download GPG key", src=key_uri, dest=asc_path)

    if asc.changed:
        server.shell(
            name=f"{name}: convert GPG key",
            commands=[f"gpg --dearmor < {asc_path} > {gpg_path}.new && mv {gpg_path}.new {gpg_path}"],
        )

    files.put(
        name=f"{name}: add Apt repository",
        src=StringIO(f"""Types: deb
URIs: {repo_uri}
Suites: {suites}
Components: {components}
Architectures: {arch}
Signed-By: {asc_path}
"""),
        dest=sources_path,
    )


GITHUB_ED25519_KNOWN_HOSTS_LINE = (
    "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl\n"
)

CODEBERG_ED25519_KNOWN_HOSTS_LINE = (
    "codeberg.org ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIVIC02vnjFyL+I4RHfvIGNtOgJMe769VTF1VR4EB3ZB\n"
)


def deploy_ssh_deploy_key(user, home, pass_path, known_hosts_line):
    """Provision an SSH deploy key for `user` so git.repo() can clone a private or rate-limited repo."""

    # Since this is normally a one-time op, we can fail and try to continue
    key = get_pass(pass_path, fail_ok=True)
    if not key.strip():
        print(
            [
                f"No deploy key at {pass_path},\n",
                "generate with: ssh-keygen -t ed25519 -f ~/.ssh/id_deploy\n",
                "register it as a deploy key on the target repo\n",
                f"save with: pass insert -m {pass_path} < ~/.ssh/id_deploy",
            ]
        )
        return

    ssh_dir = f"{home}/.ssh"
    files.directory(
        name="Create .ssh for deploy key",
        path=ssh_dir,
        user=user,
        group=user,
        mode="700",
    )
    files.put(
        name="Upload SSH deploy key",
        src=StringIO(key),
        dest=f"{ssh_dir}/id_ed25519",
        user=user,
        group=user,
        mode="600",
    )
    files.put(
        name="Upload known_hosts",
        src=StringIO(known_hosts_line),
        dest=f"{ssh_dir}/known_hosts",
        user=user,
        group=user,
        mode="644",
    )


def set_motd(content: StringIO | None = None):
    files.put(
        name="Set motd",
        src=content or StringIO(""),
        dest="/etc/motd",
        force=True,
    )


def install_uv():
    apt.packages(
        name="Install pip",
        packages=["python3-pip"],
    )
    server.shell(
        name="Install uv",
        commands=["pip install --break-system-packages --root-user-action=ignore -U uv"],
    )


def install_docker():
    codename = host.get_fact(AptCodename)

    if codename == "bookworm":
        deploy_apt_source(
            "docker", "https://download.docker.com/linux/debian", "https://download.docker.com/linux/debian/gpg"
        )

        apt.packages(
            name="Install Docker",
            packages=[
                "docker-ce",
                "docker-compose-plugin",
            ],
            update=True,
        )

    elif codename == "trixie":
        apt.packages(
            name="Install Docker",
            packages=[
                "docker.io",
                "docker-compose",
            ],
            update=True,
        )
