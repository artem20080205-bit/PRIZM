"""Stop only a subprocess started by this application, including Windows launchers."""
import psutil

def stop(process):
    pid = int(process.processId())
    if not pid:
        return
    try:
        parent = psutil.Process(pid)
        targets = parent.children(recursive=True) + [parent]
    except psutil.NoSuchProcess:
        return
    for item in targets:
        try: item.terminate()
        except psutil.NoSuchProcess: pass
    _, alive = psutil.wait_procs(targets, timeout=2)
    for item in alive:
        try: item.kill()
        except psutil.NoSuchProcess: pass
    psutil.wait_procs(alive, timeout=2)
    process.waitForFinished(1000)
