"""Archive an account without deleting its saved data."""
import os
import shutil
import time


def remove_account(s, uid):
    """Nothing is deleted: the account's folder moves to removed/<id>-<time>, and moving it back restores it."""
    uid = str(uid)
    if not uid.isdecimal() or len(uid) > 20 or not s.exists(uid):
        raise ValueError("Choose an account from this server.")
    dst = os.path.join(s.root, "removed", f"{uid}-{time.strftime('%Y%m%d-%H%M%S')}")
    original_dst = dst
    suffix = 1
    while os.path.exists(dst):
        dst = f'{original_dst}-{suffix}'
        suffix += 1
    source = os.path.join(s.root, uid)
    root = os.path.realpath(s.root)
    if any(os.path.commonpath((root, os.path.realpath(path))) != root for path in (source, dst)):
        raise ValueError('Account removal paths must stay inside accounts.')
    os.makedirs(os.path.dirname(dst), exist_ok=True); shutil.move(source, dst)
    if s.active() == uid:   # the next listed account plays; with none left, the game's next start makes a fresh one
        rest = [i for i, _, _ in s.listing()]
        if rest: s.set_active(rest[0])
        else: os.remove(os.path.join(s.root, "active.txt"))
    return dict(ok=True, id=uid, moved=dst, active=s.active())

