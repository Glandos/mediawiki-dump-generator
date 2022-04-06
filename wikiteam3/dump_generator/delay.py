import time


def delay(config: dict):
    """Add a delay if configured for that"""
    if config["delay"] > 0:
        print("Sleeping... %.2f seconds..." % (config["delay"]))
        time.sleep(config["delay"])
