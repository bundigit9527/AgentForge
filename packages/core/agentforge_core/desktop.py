import hashlib
import json


def slot_manifest(directory, owner):
    key = hashlib.sha256(json.dumps(list(owner), ensure_ascii=True).encode()).hexdigest()[:32]
    return directory / (key + ".json")
