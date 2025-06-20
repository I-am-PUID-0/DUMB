from utils.global_logger import logger
from utils.logger import format_time, get_start_time, time_to_complete
from plexapi.server import PlexServer
from plexapi import exceptions as plexapi_exceptions
from requests.exceptions import HTTPError
import requests, re, schedule, threading, time


max_retry_attempts = 5
retry_interval = 10


def delete_media_with_retry(media):
    # logger = get_logger(log_name='duplicate_cleanup')
    retry_attempt = 0
    continue_execution = True

    while retry_attempt < max_retry_attempts:
        try:
            media.delete()
            break
        except requests.exceptions.ReadTimeout:
            retry_attempt += 1
            logger.warning(
                f"Read timeout occurred. Retrying delete operation (Attempt {retry_attempt})..."
            )
            time.sleep(retry_interval)
        except plexapi_exceptions.NotFound as e:
            logger.warning(
                f"404 Not Found error occurred. Skipping delete operation for media ID: {media.id}"
            )
            continue_execution = False
            break
    else:
        logger.error(
            f"Max retry attempts reached. Unable to delete media ID: {media.id}"
        )

    return continue_execution


def process_tv_shows():
    # logger = get_logger(log_name='duplicate_cleanup')
    try:
        plex_server = PlexServer(PLEXADD, PLEXTOKEN)
        tv_section = None
        for section in plex_server.library.sections():
            if section.type == "show":
                tv_section = section
                break

        if tv_section is not None:
            logger.info(f"TV show library section: {tv_section.title}")
            duplicate_episodes = tv_section.search(duplicate=True, libtype="episode")
            episodes_to_delete = []

            for episode in duplicate_episodes:
                has_RCLONEMN = False
                has_other_directory = False
                media_id = ""
                for media in episode.media:
                    for part in media.parts:
                        if re.search(f"/{RCLONEMN}[0-9a-zA-Z_]*?/", part.file):
                            has_RCLONEMN = True
                            media_id = media.id
                        else:
                            has_other_directory = True
                    if has_RCLONEMN and has_other_directory:
                        for part in media.parts:
                            logger.info(
                                f"Duplicate TV show episode found: Show: {episode.show().title} - Episode: {episode.title} (Media ID: {media_id})"
                            )
                            episodes_to_delete.append((episode, media_id))

            if len(episodes_to_delete) > 0:
                logger.info(
                    f"Number of TV show episodes to delete: {len(episodes_to_delete)}"
                )
            else:
                logger.info("No duplicate TV show episodes found.")

            for episode, media_id in episodes_to_delete:
                for media in episode.media:
                    if media.id == media_id:
                        for part in media.parts:
                            logger.info(
                                f"Deleting TV show episode from Rclone directory: {episode.show().title} - {episode.title} (Media ID: {media_id})"
                            )
                            continue_execution = delete_media_with_retry(media)
                            if not continue_execution:
                                break
                        if not continue_execution:
                            break
        else:
            logger.error("TV show library section not found.")
    except requests.exceptions.ConnectionError as e:
        logger.error(
            f"Connection error occurred while processing TV show library section: {str(e)}"
        )
    except Exception as e:
        logger.error(
            f"Error occurred while processing TV show library section: {str(e)}"
        )


def process_movies():
    # logger = get_logger(log_name='duplicate_cleanup')
    try:
        plex_server = PlexServer(PLEXADD, PLEXTOKEN)
        movie_section = None
        for section in plex_server.library.sections():
            if section.type == "movie":
                movie_section = section
                break

        if movie_section is not None:
            logger.info(f"Movie library section: {movie_section.title}")
            duplicate_movies = movie_section.search(duplicate=True, libtype="movie")
            movies_to_delete = []
            encountered_404_error = False

            for movie in duplicate_movies:
                if encountered_404_error:
                    logger.warning(
                        "Skipping remaining episodes due to previous 404 error."
                    )
                    break
                has_RCLONEMN = False
                has_other_directory = False
                media_id = ""
                for media in movie.media:
                    for part in media.parts:
                        if re.search(f"/{RCLONEMN}[0-9a-zA-Z_]*?/", part.file):
                            has_RCLONEMN = True
                            media_id = media.id
                        else:
                            has_other_directory = True
                    if has_RCLONEMN and has_other_directory:
                        for part in media.parts:
                            logger.info(
                                f"Duplicate movie found: {movie.title} (Media ID: {media_id})"
                            )
                            movies_to_delete.append((movie, media_id))

            if len(movies_to_delete) > 0:
                logger.info(f"Number of movies to delete: {len(movies_to_delete)}")
            else:
                logger.info("No duplicate movies found.")

            for movie, media_id in movies_to_delete:
                for media in movie.media:
                    if media.id == media_id:
                        for part in media.parts:
                            logger.info(
                                f"Deleting movie from Rclone directory: {movie.title} (Media ID: {media_id})"
                            )
                            continue_execution = delete_media_with_retry(media)
                            if not continue_execution:
                                break
                        if not continue_execution:
                            break
        else:
            logger.error("Movie library section not found.")
    except requests.exceptions.ConnectionError as e:
        logger.error(
            f"Connection error occurred while processing movie library section: {str(e)}"
        )
    except Exception as e:
        logger.error(f"Error occurred while processing movie library section: {str(e)}")


def setup():
    try:
        app_env_variables = {
            "PLEX_ADDRESS": PLEXADD,
            "PLEX_TOKEN": PLEXTOKEN,
            "RCLONE_MOUNT_NAME": RCLONEMN,
        }

        logger.info("Checking required duplicate cleanup environment variables.")
        for var_name, value in app_env_variables.items():
            if value is None:
                logger.error(
                    f"Application environment variable '{var_name}' is not set."
                )
            else:
                logger.debug(f"Application environment variable '{var_name}' is set.")

        if all(app_env_variables.values()):
            if DUPECLEAN is not None and cleanup_interval() == 24:
                logger.info("Duplicate cleanup interval missing")
                logger.info("Defaulting to " + format_time(cleanup_interval()))
                cleanup_thread()
            elif DUPECLEAN is not None:
                logger.info(
                    "Duplicate cleanup interval set to "
                    + format_time(cleanup_interval())
                )
                cleanup_thread()
    except Exception as e:
        logger.error(e)


def cleanup_interval():
    if CLEANUPINT is None:
        interval = 24
    else:
        interval = float(CLEANUPINT)
    return interval


def cleanup_schedule():
    time.sleep(60)
    interval = cleanup_interval()
    interval_minutes = int(interval * 60)
    schedule.every(interval_minutes).minutes.do(start_cleanup)
    while True:
        schedule.run_pending()
        time.sleep(1)


def start_cleanup():
    logger.info("Starting duplicate cleanup")
    start_time = get_start_time()
    process_tv_shows()
    process_movies()
    total_time = time_to_complete(start_time)
    logger.info("Duplicate cleanup complete.")
    logger.info(f"Total time required: {total_time}")


def cleanup_thread():
    thread = threading.Thread(target=cleanup_schedule)
    thread.daemon = True
    thread.start()
