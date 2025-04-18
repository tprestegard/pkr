# Copyright© 1986-2024 Altair Engineering Inc.

# pylint: disable=C0111,E1101,R0912,R0913

"""Utils functions for pkr"""

import hashlib
from builtins import input
from builtins import range
from builtins import str
from enum import Enum
from fnmatch import fnmatch
from glob import glob
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import time
import platform

from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto import Random
import docker
import jinja2
from passlib.apache import HtpasswdFile

ENV_FOLDER = "env"
KARD_FOLDER = "kard"
PATH_ENV_VAR = "PKR_PATH"


class Cmd(Enum):
    OTHER = 0
    ENCRYPT = 1
    DECRYPT = 2


class PkrException(Exception):
    """pkr Exception"""


class KardInitializationException(PkrException):
    """pkr Exception"""


class PasswordException(PkrException):
    def __init__(
        self, message="Encryption password is not specified (use global -p <password> option)"
    ):
        super().__init__(message)


# pylint: disable=C0415
def is_pkr_path(path):
    """Check environments files to deduce if path is a usable pkr path"""
    return path.is_dir() and len(list(path.glob(f"{ENV_FOLDER}/*/env.yml"))) > 0


def get_pkr_path(raise_if_not_found=True):
    """Return the path of the pkr folder

    If the env. var 'PKR_PATH' is specified, it is returned, otherwise a
    KeyError exception is raised.
    """

    full_path = Path(os.environ.get(PATH_ENV_VAR, os.getcwd())).absolute()
    pkr_path = full_path
    while pkr_path.parent != pkr_path:
        if is_pkr_path(pkr_path):
            return pkr_path
        pkr_path = pkr_path.parent

    if raise_if_not_found and not is_pkr_path(pkr_path):
        raise KardInitializationException(
            f"{'Given' if PATH_ENV_VAR in os.environ else 'Current'} path {full_path} is not a "
            f"valid pkr path, no usable env found"
        )

    return pkr_path


def get_kard_root_path():
    """Return the root path of Kards"""
    return get_pkr_path() / KARD_FOLDER


def get_timestamp():
    """Return a string timestamp"""
    return time.strftime("%Y%m%d-%H%M%S")


class HashableDict(dict):
    """Extends dict with a __hash__ method to make it unique in a set"""

    def __key(self):
        return json.dumps(self)

    def __hash__(self):
        return hash(self.__key())

    def __eq__(self, other):
        return self.__key() == other.__key()  # pylint: disable=W0212


def merge(source, destination, overwrite=True):
    """Deep merge 2 dicts

    Warning: the source dict is merged INTO the destination one. Make a copy
    before using it if you do not want to destroy the destination dict.
    """
    if not source:
        return destination
    if destination is None:
        destination = {}
    for key, value in list(source.items()):
        if isinstance(value, dict):
            # Handle type mismatch
            if overwrite and not isinstance(destination.get(key), dict):
                destination[key] = {}
            # get node or create one
            node = destination.setdefault(key, {})
            merge(value, node, overwrite)
        elif isinstance(value, list):
            if key in destination:
                # Handle type mismatch
                if overwrite and not isinstance(destination[key], list):
                    destination[key] = []
                try:
                    destination[key] = list(dict.fromkeys(destination[key] + value))
                # Prevent errors when having unhashable dict types
                except TypeError:
                    destination[key].extend(value)
            else:
                destination[key] = value
        elif overwrite or key not in destination:
            destination[key] = value

    return destination


def diff(previous, current):
    """Deep diff 2 dicts

    Return a dict of new elements in the `current` dict.
    Does not handle removed elements.
    """
    result = {}

    for key in current.keys():
        value = current[key]
        p_value = previous.get(key, None)
        if p_value is None:
            result[key] = value
            continue

        if isinstance(value, dict):
            difference = diff(previous.get(key, {}), value)
            if difference:
                result[key] = difference

        elif isinstance(value, list):
            value = [x for x in value if x not in p_value]
            if value != []:
                result[key] = value

        elif value != p_value:
            result[key] = value

    return result


def generate_password(pw_len=15):
    """Generate a password"""
    alphabet = "abcdefghijklmnopqrstuvwxyz"
    upperalphabet = alphabet.upper()
    pwlist = []

    for _ in range(pw_len // 3):
        pwlist.append(secrets.choice(alphabet))
        pwlist.append(secrets.choice(upperalphabet))
        pwlist.append(str(secrets.randbelow(10)))
    for _ in range(pw_len - len(pwlist)):
        pwlist.append(secrets.choice(alphabet))

    secrets.SystemRandom().shuffle(pwlist)
    return "".join(pwlist)


def ask_input(name):
    return input(f"Missing meta({name}):")


class TemplateEngine:
    def __init__(self, tpl_context):
        """Init templating context (filters and functions)"""
        self.tpl_context = tpl_context.copy()

        self.pkr_path = get_pkr_path()
        self.tpl_env = jinja2.Environment(
            extensions=["jinja2_ansible_filters.AnsibleCoreFiltersExtension"],
            loader=jinja2.FileSystemLoader(str(self.pkr_path)),
        )

        def sha256(string):
            return hashlib.sha256(string.encode("utf-8")).hexdigest()

        self.tpl_env.filters["sha256"] = sha256

        def format_htpasswd(username, password):
            ht = HtpasswdFile()
            ht.set_password(username, password)
            return ht.to_string().rstrip().decode("utf-8")

        self.tpl_context["format_htpasswd"] = format_htpasswd

    def process_template(self, template_file):
        """Process a template and render it in the context

        Args:
          - template_file: the template to load

        Return the result of the processed template.
        """

        rel_template_file = str(template_file.relative_to(self.pkr_path))

        template = self.tpl_env.get_template(rel_template_file)
        out = template.render(self.tpl_context)
        return out

    def process_string(self, string):
        """Process a string and render it in the context

        Args:
          - template_file: the string to template

        Return the result of the processed string.
        """
        template = self.tpl_env.from_string(string)
        out = template.render(self.tpl_context)
        return out

    def copy(self, path, origin, local_dst, excluded_paths, gen_template=True):
        """Copy a tree recursively, while excluding specified files

        Args:
          - path: the file or folder to copy
          - origin: the base folder of all paths
          - local_dst: the destination folder / file
          - excluded_paths: the list of unwanted excluded files
        """
        path_str = str(path)
        if "*" in path_str:
            file_list = [Path(p) for p in glob(path_str)]
            if "*" in origin.name:
                origin = origin.parent

            for path_it in file_list:
                rel_local_dst = path_it.relative_to(origin)
                full_local_dst = local_dst / rel_local_dst

                self.copy(path_it, path_it, full_local_dst, excluded_paths, gen_template)
        elif path.is_file():
            # Direct match for excluded paths
            if path in excluded_paths:
                return
            if path != origin:
                # path = /pkr/src/backend/api/__init__.py
                abs_path = path.relative_to(origin)
                # path = api/__init__.py
                dst_path = local_dst / abs_path
                # path = docker-context/backend/api/__init__.py
            else:
                # Here we avoid having a '.' as our abs_path
                dst_path = local_dst
            # We ensure that the containing folder exists

            dst_path.parent.mkdir(parents=True, exist_ok=True)
            if gen_template and path.name.endswith(".template"):
                # If the dst_local contains the filename with template
                if not dst_path.is_dir():
                    if dst_path.name.endswith(".template"):
                        dst_path = self.remove_ext(dst_path)
                else:  # We create the destination path
                    dst_path = dst_path / self.remove_ext(path.name)
                out = self.process_template(path)
                dst_path.write_text(out)
                shutil.copystat(path_str, str(dst_path))
                # os.chmod(str(dst_path), 0o600) # make invisible to the world
            else:
                shutil.copy2(path_str, str(dst_path))
        elif path.is_dir():
            for path_it in path.iterdir():
                path_it = path / path_it
                if not any((fnmatch(str(path_it), str(exc_path)) for exc_path in excluded_paths)):
                    self.copy(path_it, origin, local_dst, excluded_paths, gen_template)

    @staticmethod
    def remove_ext(path):
        """Remove the portion of a string after the last dot."""
        return path.parent / path.stem


FLAGS = re.VERBOSE | re.MULTILINE | re.DOTALL
WHITESPACE = re.compile(r"[ \t\n\r]*", FLAGS)


class ConcatJSONDecoder(json.JSONDecoder):
    def decode(self, s, _w=WHITESPACE.match):
        s_len = len(s)

        objs = []
        end = 0
        while end != s_len:
            obj, end = self.raw_decode(s, idx=_w(s, end).end())
            end = _w(s, end).end()
            objs.append(obj)
        return objs


def get_current_container():
    """Return container inspect if we run in docker, None otherwise"""
    if platform.system() != "Linux":
        return None
    with Path("/proc/self/cgroup").open(encoding="utf-8") as cgroup_file:
        for line in cgroup_file:
            if "docker" in line:
                container_id = line[line.rindex("/") + 1 :].strip()
                md = re.match(r"docker-(.+)\.scope$", container_id)
                if md:
                    container_id = md.group(1)
                cli = docker.DockerClient(version="auto")  # Default to /var/run/docker.sock
                return cli.containers.get(container_id)
    return None


def ensure_key_present(key, default, data, path=None):
    """Ensure that a key is present, set the default is present, or ask
    the user to input it."""

    if key in data:
        return data.get(key)
    if key in default:
        return default.get(key)

    return ask_input((path or "") + key)


def ensure_definition_matches(definition, defaults, data, path=None):
    """Recursive function that ensures data is provided.

    Ask for it if not.
    """
    path = path or ""
    if isinstance(definition, dict):
        values = {
            k: ensure_definition_matches(
                definition=v,
                defaults=defaults.get(k, []),
                data=data.get(k, {}),
                path=path + k + "/",
            )
            for k, v in definition.items()
            if v is not None
        }
        values.update(
            {
                k: ensure_key_present(k, defaults, data, path)
                for k, v in definition.items()
                if v is None
            }
        )
        return values

    if isinstance(definition, list):
        values = {}
        for element in definition:
            values.update(
                ensure_definition_matches(
                    definition=element, defaults=defaults, data=data, path=path
                )
            )
        return values

    value = ensure_key_present(definition, defaults, data, path)
    return {definition: value}


def create_pkr_folder(pkr_path=None):
    """Creates a folder structure for pkr.

    This looks like:
    PKR_PATH/
    ├── env/
    │   └── dev/
    │       └── env.yaml
    └── kard/
    """
    pkr_path = pkr_path or get_pkr_path(False)

    (pkr_path / "env" / "dev").mkdir(parents=True)
    (pkr_path / "env" / "dev" / "env.yml").touch()
    (pkr_path / "kard").mkdir(parents=True)


def dedup_list(src):
    """Dedup src list (in-place) and yield duplicates"""
    for item in set(src):
        if src.count(item) != 1:
            yield item
            src.remove(item)


def merge_lists(src, dest, insert=True):
    """Merge lists avoiding duplicates"""
    if insert:
        for x in reversed(src):
            if x in dest:
                continue
            dest.insert(0, x)
    else:
        dest.extend([x for x in src if x not in dest])
    return dest


def encrypt_swap(file, file_enc, password):
    enc = encrypt_file(file, password)
    with file_enc.open("wb") as fe:
        os.chmod(file_enc, 0o600)
        fe.write(enc)
        file.unlink()


def encrypt_file(file, password=None):
    if password is None:
        raise PasswordException()
    with file.open("rb") as fl:
        data = fl.read()
        data_enc = encrypt_with_key(password.encode("utf-8"), data)
        return data_enc


def decrypt_swap(file, file_enc, password):
    data = decrypt_file(file_enc, password)
    file.write_text(data.decode("utf-8"))
    os.chmod(file, 0o600)
    file_enc.unlink()


def decrypt_file(file, password=None):
    if password is None:
        raise PasswordException()
    with file.open("rb") as fl:
        data_enc = fl.read()
        data = decrypt_with_key(password.encode("utf-8"), data_enc)
        return data


def encrypt_with_key(key: bytes, source: bytes) -> bytes:
    key = SHA256.new(key).digest()  # use SHA-256 over our key to get a proper-sized AES key
    i_v = Random.new().read(AES.block_size)  # generate i_v
    encryptor = AES.new(key, AES.MODE_CBC, i_v)
    padding = AES.block_size - len(source) % AES.block_size  # calculate needed padding
    src = b"".join([source, bytes([padding]) * padding])
    data = i_v + encryptor.encrypt(src)  # store the i_v at the beginning and encrypt
    return data


def decrypt_with_key(key: bytes, source: bytes) -> bytes:
    key = SHA256.new(key).digest()  # use SHA-256 over our key to get a proper-sized AES key
    i_v = source[: AES.block_size]  # extract the i_v from the beginning
    decryptor = AES.new(key, AES.MODE_CBC, i_v)
    data = decryptor.decrypt(source[AES.block_size :])  # decrypt
    padding = data[-1]  # pick the padding value from the end
    if data[-padding:] != bytes([padding]) * padding:
        raise Exception("Incorrect decryption password")
    return data[:-padding]  # remove the padding
