import click
from job_search.core.config import load_config
from job_search.utils.logging import setup_logging


@click.command()
@click.option("--config", default="config/config.yaml", show_default=True, help="Path to config file")
@click.option("--resume", is_flag=True, default=False, help="Resume from last checkpoint")
@click.option("--log-level", default=None, help="Override log level (DEBUG, INFO, WARNING, ERROR)")
def main(config: str, resume: bool, log_level: str | None) -> None:
    """AI-Assisted Job Search Tool"""
    cfg = load_config(config)
    setup_logging(level=log_level or cfg.logging.level, log_file=cfg.logging.file)

    from job_search.orchestration.coordinator import JobSearchCoordinator

    coordinator = JobSearchCoordinator(cfg)
    try:
        coordinator.start(resume=resume)
    except KeyboardInterrupt:
        pass
    finally:
        coordinator.cleanup()


if __name__ == "__main__":
    main()
