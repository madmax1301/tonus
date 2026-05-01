import os
from pathlib import Path
from typing import Optional

def get_download_path(track_info: dict, base_dir: str, extension: str = "mp3", track_id: str = None) -> str:
    """Generate a safe download path for a track"""
    artist = sanitize_filename(track_info.get('artist', 'Unknown Artist'))
    title = sanitize_filename(track_info.get('name', 'Unknown Title'))
    
    # Create filename: Artist - Title.mp3
    filename = f"{artist} - {title}.{extension}"
    
    # Ensure base directory exists
    Path(base_dir).mkdir(parents=True, exist_ok=True)
    
    return os.path.join(base_dir, filename)

def sanitize_filename(filename: str) -> str:
    """Remove invalid characters from filename"""
    import re
    # Remove invalid characters
    filename = re.sub(r'[<>:"/\\|?*]', '', filename)
    # Replace multiple spaces with single space
    filename = re.sub(r'\s+', ' ', filename)
    # Trim
    return filename.strip()

def cleanup_file(file_path: str) -> bool:
    """Delete a file if it exists"""
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            return True
        return False
    except Exception as e:
        print(f"Error deleting file {file_path}: {e}")
        return False

