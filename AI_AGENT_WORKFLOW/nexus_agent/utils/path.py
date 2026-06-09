from pathlib import Path


# in this base is the current working directory of the tool that is being executed and path is the path that is being passed to the tool when it is being executed. This function will resolve the path to an absolute path by joining the base and the path if the path is not absolute. If the path is already absolute then it will return the path as it is.
def resolve_path(base: str | Path, path: str | Path):
    path = Path(path)# so if it is a strnig it will convert to path and if it is already a path it will do nothing
    if path.is_absolute():
        return path.resolve()# it will resolve the path to an absolute path by joining the base and the path if the path is not absolute. If the path is already absolute then it will return the path as it is.

    return Path(base).resolve() / path #example if base is /home/user and path is documents/file.txt then it will return /home/user/documents/file.txt

def display_path_to_cwd(path:str,cwd:Path | None) -> str:
    try:
        p = Path(path)
    except Exception:
        return path
    
    if cwd:
        try:
            return str(p.relative_to(cwd))
        except ValueError:
            pass
    return str(p)

def ensure_parent_directory(path: str | Path) -> Path:
    path = Path(path)
    
    path.parent.mkdir(parents=True,exist_ok=True)
    
    return path

def is_binary_file(file_path:str | Path) -> bool:
    #this function will check if the file is a binary file or not by reading the first 8191 bytes of the file and checking if there are any null bytes in it. If there are null bytes in the first 8191 bytes of the file then it is a binary file and if there are no null bytes then it is a text file.
    try:
        with open(file_path, 'rb') as f:
            chunk = f.read(8191) #read the first 8191 bytes of the file
            return b"\x00" in chunk #this checks if there are any null bytes in the first 8191 bytes of the file. If there are null bytes then it is a binary file and if there are no null bytes then it is a text file.
    except (OSError, IOError) as e: 
        # If there is an error opening the file, we can choose to treat it as non-binary
        return False
