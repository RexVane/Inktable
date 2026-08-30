def main() -> None:
    """Start the same authenticated sidecar used by the desktop application."""
    import multiprocessing

    from app.entrypoint import main as run_sidecar

    multiprocessing.freeze_support()
    run_sidecar()
