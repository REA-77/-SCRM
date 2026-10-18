"""Cookie management for persisting Zhihu login sessions."""

from __future__ import annotations

import json
import os
from pathlib import Path

from loguru import logger


def get_cookies_dir() -> Path:
    """Get the cookies directory, creating it if needed."""
    cookies_dir = Path(__file__).parent.parent / "cookies"
    cookies_dir.mkdir(exist_ok=True)
    return cookies_dir


def get_cookies_path() -> Path:
    """Get the path to the cookies file.

    Priority:
    1. COOKIES_PATH env var
    2. ./cookies/cookies.json (default)
    """
    env_path = os.environ.get("COOKIES_PATH")
    if env_path:
        return Path(env_path)
    return get_cookies_dir() / "cookies.json"


def load_cookies() -> list[dict] | None:
    """Load cookies from environment variable or disk. Returns None if no cookies exist."""
    # First try environment variable
    env_cookies = os.environ.get("ZHIHU_COOKIES_JSON")
    if env_cookies:
        try:
            data = json.loads(env_cookies)
            logger.info(f"Loaded {len(data)} cookies from environment variable")
            return data
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse ZHIHU_COOKIES_JSON: {e}")
    
    # Fallback to file
    path = get_cookies_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        logger.info(f"Loaded {len(data)} cookies from {path}")
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to load cookies: {e}")
        return None


def save_cookies(cookies: list[dict]) -> None:
    """Save cookies to environment variable and disk."""
    # Save to environment variable
    os.environ["ZHIHU_COOKIES_JSON"] = json.dumps(cookies, ensure_ascii=False)
    
    # Also save to file for persistence
    path = get_cookies_path()
    path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"Saved {len(cookies)} cookies to {path} and environment variable")


def delete_cookies() -> bool:
    """Delete the cookies file and clear environment variable. Returns True if deleted."""
    # Clear environment variable
    if "ZHIHU_COOKIES_JSON" in os.environ:
        del os.environ["ZHIHU_COOKIES_JSON"]
        logger.info("Cleared ZHIHU_COOKIES_JSON from environment")
    
    # Delete file
    path = get_cookies_path()
    if path.exists():
        path.unlink()
        logger.info(f"Deleted cookies at {path}")
        return True
    return False
