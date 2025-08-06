import os

# Rimuove le cartelle vuote da media/
def remove_empty_dirs(path, stop_at=None):

    path = os.path.abspath(path)
    stop_at = os.path.abspath(stop_at) if stop_at else None

    while path != stop_at:
        if not os.path.isdir(path) or os.listdir(path):
            break
        os.rmdir(path)
        path = os.path.dirname(path)