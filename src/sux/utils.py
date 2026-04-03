import os
import pwd


def host_username():
    """Get the current host username."""
    return pwd.getpwuid(os.getuid()).pw_name
