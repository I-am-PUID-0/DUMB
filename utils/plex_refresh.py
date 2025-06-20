import os
import sys
import time
from plexapi.server import PlexServer
from pathlib import Path

plex_refresh_path = Path(__file__).resolve()
project_path = plex_refresh_path.parent.parent.parent
sys.path.append(str(project_path))
from base import PLEXTOKEN, PLEXADD, PLEXMOUNT, RCLONEMN, RCLONEDIR


# Configuration
plex_url = PLEXADD.replace("'", "").replace('"', "")
token = PLEXTOKEN.replace("'", "").replace('"', "")
plex_mount = PLEXMOUNT.replace("'", "").replace('"', "")
zurg_mount = f"{RCLONEDIR}/{RCLONEMN}"
zurg_timeout = 300  # 5 minutes in seconds for Zurg file availability
plex_timeout = 60  # Maximum time to wait for Plex to process the refresh
wait_increment = 1  # Time increment for each wait step
max_retries = 20  # Maximum number of retries for Plex refresh

### Do not alter below ###
plex = PlexServer(plex_url, token)


def refresh_sections(section_ids, filepath):
    for section_id in section_ids:
        section = plex.library.sectionByID(section_id)
        try:
            section.update(path=filepath)
            print(f"Refreshed section ID {section_id} with path {filepath}")
        except Exception as e:
            print(f"Error refreshing section ID {section_id}: {e}")


def check_path_in_plex(path, section_ids):
    try:
        for section_id in section_ids:
            section = plex.library.sectionByID(section_id)
            recent_items = section.recentlyAdded(maxresults=50)
            for item in recent_items:
                if section.TYPE == "movie":
                    for media in item.media:
                        for part in media.parts:
                            if path in part.file:
                                return True
                elif section.TYPE == "show":
                    for episode in item.episodes():
                        for media in episode.media:
                            for part in media.parts:
                                if path in part.file:
                                    return True
        return False
    except Exception as e:
        print(f"Error during search: {e}")
        return False


def main():
    section_ids = [section.key for section in plex.library.sections()]
    valid_directories = {}

    for arg in sys.argv[1:]:
        print(f"Starting Plex Update for {arg}")
        directory_name = arg.split("/")[0]
        directory_path = os.path.join(plex_mount, directory_name)
        directory_exists = any(
            directory_path in section.locations for section in plex.library.sections()
        )

        if directory_exists:
            valid_directories[arg] = directory_path
            print(
                f"Directory path {directory_path} exists in Plex. Will process {arg}."
            )
        else:
            print(
                f"Directory path {directory_path} does not exist in Plex. Skipping {arg}."
            )

    for arg, path in valid_directories.items():
        zurg_arg = os.path.join(zurg_mount, arg)
        elapsed_time = 0
        while not os.path.exists(zurg_arg) and elapsed_time < zurg_timeout:
            print(
                f"Waiting for {arg} to be available at {zurg_mount}... (Timeout in {zurg_timeout - elapsed_time} seconds)"
            )
            time.sleep(10)
            elapsed_time += 10

        if not os.path.exists(zurg_arg):
            print(f"{arg} not found in {zurg_mount}. Skipping.")
            continue

        plex_arg = os.path.join(plex_mount, arg)
        retry_count = 0
        verified = False
        while not verified and retry_count < max_retries:
            refresh_sections(section_ids, plex_arg)
            total_wait_time = 0

            while total_wait_time < plex_timeout:
                print(
                    f"Waiting for {wait_increment} seconds for Plex to process the refresh (Total waited: {total_wait_time} seconds)..."
                )
                time.sleep(wait_increment)
                total_wait_time += wait_increment

                if check_path_in_plex(plex_arg, section_ids):
                    print(f"Verification successful: {plex_arg} found in Plex library.")
                    verified = True
                    break

            if not verified:
                print(
                    f"Verification unsuccessful for {plex_arg}. Retrying ({retry_count + 1}/{max_retries})..."
                )
                retry_count += 1

        if not verified:
            print("All retries failed. Verification unsuccessful for", plex_arg)


if __name__ == "__main__":
    main()
