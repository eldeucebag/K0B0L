import time
from typing import Optional, Dict, Any

class Cache:
    """A simple in-memory cache with TTL (Time To Live) support."""
    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}

    def set(self, key: str, value: Any, ttl: int = 300):
        """Sets a key-value pair with a specific TTL (in seconds)."""
        # Store the value along with its creation time and expiration time
        self._cache[key] = {
            "value": value,
            "expiry_time": time.time() + ttl
        }

    def get(self, key: str) -> Optional[Any]:
        """Retrieves a value if it is not expired. Returns None if the key is missing or expired."""
        if key not in self._cache:
            return None

        item = self._cache[key]
        current_time = time.time()

        if current_time > item["expiry_time"]:
            # The item has expired, remove it from cache and return None
            del self._cache[key]
            return None
        
        return item["value"]

    def clear(self):
        """Clears all items from the cache."""
        self._cache.clear()

    def size(self) -> int:
        """Returns the number of items currently in the cache."""
        return len(self._cache)