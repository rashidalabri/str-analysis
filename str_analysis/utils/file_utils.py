import gzip
import hailtop.fs as hfs
import io
import os
import requests
import tempfile

gcloud_requester_pays_project = None

def set_requester_pays_project(project):
    """Sets the requester pays project for all hailtop.fs calls"""
    global gcloud_requester_pays_project
    gcloud_requester_pays_project = project


def open_file(path, download_local_copy_before_opening=False, gunzip=False, is_text_file=False):
    if path.startswith("gs://") and download_local_copy_before_opening:
        path = download_local_copy(path)

    path = os.path.expanduser(path)
    mode = "r"
    if path.startswith("gs://"):
        file = hfs.open(path, f"{mode}b", requester_pays_config=gcloud_requester_pays_project)
        if gunzip or path.endswith("gz"):
            file = gzip.GzipFile(fileobj=file, mode=mode)
    else:
        if gunzip or path.endswith("gz"):
            file = gzip.open(path, mode=mode)
        else:
            if is_text_file:
                file = open(path, f"{mode}t", encoding="utf-8")
            else:
                file = open(path, mode="rb")
            return file

    return io.TextIOWrapper(file, encoding="utf-8")


def file_exists(path):
    if path.startswith("gs://"):
        return hfs.exists(path, requester_pays_config=gcloud_requester_pays_project)

    path = os.path.expanduser(path)
    return os.path.isfile(path)


def get_file_size(path):
    if path.startswith("gs://"):
        return hfs.stat(path, requester_pays_config=gcloud_requester_pays_project).size
    else:
        return os.path.getsize(os.path.expanduser(path))


def download_local_copy(url_or_google_storage_path):
    """Downloads the given URL or gs:// path to a local temp file and returns the path to the local file."""

    temp_dir = tempfile.gettempdir()
    if url_or_google_storage_path.startswith("gs://"):
        path = os.path.join(temp_dir, os.path.basename(url_or_google_storage_path))
        if not os.path.isfile(path):
            print(f"Downloading {url_or_google_storage_path} to {path}")
            hfs.copy(url_or_google_storage_path, path, requester_pays_config=gcloud_requester_pays_project)
    else:
        path = os.path.join(temp_dir, os.path.basename(url_or_google_storage_path))
        if not os.path.isfile(path):
            print(f"Downloading {url_or_google_storage_path} to {path}")
            r = requests.get(url_or_google_storage_path)
            with open(path, "wb") as f:
                f.write(r.content)

    return path


