from flask import Flask, jsonify, render_template_string, request, session
from flask_session import Session
import subprocess
import re
import os
from datetime import datetime
import json
import tarfile
import logging
import time
import pty

app = Flask(__name__)

# Configure server-side session storage
app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_FILE_DIR"] = "./flask_session"
Session(app)

# Paths to fetch archives from
PATHS = [
    "/media/dfsP/MOLPATHO/_Sequences_NGS_Patho/NextSeq/",
    "/media/dfsM/_Sequences_NGS_Patho/NextSeq/",
    "/media/dfsM/_Sequences_NGS_Patho/_NovaSeq/",
    "/media/dfsP/MOLPATHO/_Sequences_NGS_Patho/MiniSeq/",
    "/media/dfsO/MiniSeq/",
    "/media/dfsP/HAEMATOPATHO/NGS/",
    "/media/dfsO/MiSeq/NGS/",
    "/media/dfsP/MOLPATHO/_Sequences_CoreFacility_HTG/",
    "/media/dfsM/_Sequences_CoreFacility_HTG/",

]

# Regular expression to parse archives
ARCHIVE_REGEX = re.compile(
    r"\s*(?P<size>[0-9,]+\s*[BK]{1,2})\s+"
    r"(?P<archive_date>\d{2}\.\d{2}\.\d{4})\s+"
    r"(?P<time>\d{2}:\d{2}:\d{2})\s+"
    r"(?P<file_path>[^\s]+)\s+"
    r"(?P<expires_on>\d{2}\.\d{2}\.\d{4})\s+"
    r"(?P<description>.+)"
)

ARCHIVE_FILE_DIR = "/var/www/html/public/test"

# Create logs directory if not exists
os.makedirs("logs", exist_ok=True)

# Configure logging
logging.basicConfig(
    filename="logs/project.log",
    filemode="a",  # append mode
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO  # Change to DEBUG for more details
)

def fetch_archives():
    """Fetch archive data using dsmc command."""
    archives = []
    for path in PATHS:
        logging.info(f"Querying archives for path: {path}")
        try:
            result = subprocess.run(
                ["dsmc", "q", "archive", "-server=ispsrv22", "-subdir=yes", path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            if result.stderr:
                logging.warning(f"DSM command warning for {path}: {result.stderr.strip()}")

            for line in result.stdout.splitlines():
                match = ARCHIVE_REGEX.match(line)
                if match:
                    data = match.groupdict()
                    try:
                        size_value, size_unit = data["size"].replace(",", "").split()
                        size_bytes = int(size_value) * (1024 if size_unit == "KB" else 1)
                        size_gb = round(size_bytes / (1024**3), 2)
                        data["size_mb"] = round(size_bytes / (1024 * 1024), 2)
                        data["size_kb"] = round(size_bytes / 1024, 2)
                        data["size_gb"] = size_gb
                        data["archive_name"] = data["file_path"].split("/")[-1].replace(".tar.gz", "")
                        archives.append(data)
                    except Exception as parse_error:
                        logging.error(f"Error parsing archive data from line: {line} | Error: {parse_error}")
        except Exception as e:
            logging.error(f"Error fetching archives from {path}: {e}")
    logging.info(f"Total archives fetched: {len(archives)}")
    return archives

def save_archives_to_file(archives):
    """Save archives to a file with a timestamped filename."""
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"{ARCHIVE_FILE_DIR}/archive_fetch_{timestamp}.txt"

    for file in os.listdir(ARCHIVE_FILE_DIR):
        if file.startswith("archive_fetch_") and file.endswith(".txt"):
            os.remove(os.path.join(ARCHIVE_FILE_DIR, file))

    try:
        with open(filename, "w") as file:
            for archive in archives:
                file.write(f"{archive}\n")
        print(f"Archives saved to {filename}")
        logging.info(f"Archives saved to {filename}")
    except Exception as e:
        print(f"Error saving archives to file: {e}")
        logging.info(f"Error saving archives to file: {e}")

def load_archives_from_last_file():
    """Load archives from the most recent saved file."""
    last_saved_file = None
    for file in os.listdir(ARCHIVE_FILE_DIR):
        if file.startswith("archive_fetch_") and file.endswith(".txt"):
            file_path = os.path.join(ARCHIVE_FILE_DIR, file)
            if not last_saved_file or os.path.getmtime(file_path) > os.path.getmtime(last_saved_file):
                last_saved_file = file_path

    if last_saved_file:
        with open(last_saved_file, "r") as file:
            return [eval(line.strip()) for line in file.readlines()]
    return []

@app.route("/check-retrieved", methods=["POST"])
def check_retrieved_archives():
    data = request.get_json()
    selected_archives = data.get("archives", [])
    
    already_retrieved = []
    logging.info(f"Checking already retrieved archives")

    for archive_name in selected_archives:
        archive_path = next(
            (archive["file_path"] for archive in session.get("archives", []) if archive["archive_name"] == archive_name),
            None,
        )
        logging.info(f"following is archive path: {archive_path}")
        retrieve_path = os.path.join(archive_path)
        if os.path.exists(archive_path):
            already_retrieved.append(archive_name)
    logging.info(f"Following archives are already retrieved: {already_retrieved}")
    return jsonify({"alreadyRetrieved": already_retrieved})

def size_to_mb(size):
    """
    Accepts:
    - "616.58 MB"
    - "1.2 GB"
    - 616.58   (assumed MB)
    """
    # Case 1: already numeric → assume MB
    if isinstance(size, (int, float)):
        return float(size)

    # Case 2: string with unit
    value = size.strip().split()
    value = float(value.replace(",", ""))
    return value

def calculate_retrieval_percentage(retrieved_value,total_value):
    """
    Returns percentage of retrieved size out of total size.
    """
    total = size_to_mb(float(total_value))
    if total <= 0:
        return 0.0

    percentage = (retrieved_value / total) * 100
    return round(percentage, 2)



@app.route("/retrieve", methods=["POST"])
def retrieve_archives():

    global progress_log
    progress_log = []
    """Retrieve selected archives using dsmc retrieve."""
    logging.info(f"inside retrieve archive function")
    data = request.get_json()
    selected_archives = data.get("archives", [])
    forceRetrieveArchives = data.get("forceArchives", [])
    logging.info(f"Following are selected archives {selected_archives} and following are force retrieve archives {forceRetrieveArchives}")
    extract_archive = data.get("extract", False)
    removeArchive = data.get("remove", False)
    extract_path = data.get("extract_Path")
    logging.info(f"Following is extraction path: {extract_path}")

    already_retrieved = []
    successfully_retrieved = []
    extraction_success = []
    extraction_errors = []
    successfully_removed = []
    not_exist = []
    failed_removals = []
    retrieve_success = False
    extract_success1 = False
    remove_success1 = False




    total_archive_size = 0.0
    total_archive_size_retrieved = 0.0

    for archive_name in selected_archives:
        archive_size = next(
            (
                archive["size_gb"]
                for archive in session.get("archives", [])
                if archive["archive_name"] == archive_name
            ),
            None
        )

        if archive_size is not None:
            total_archive_size += size_to_mb(archive_size)



    for archive_name in selected_archives:
        master, slave = pty.openpty()
        last_reported_amount = 0.0
        archive_path = next(
            (archive["file_path"] for archive in session.get("archives", []) if archive["archive_name"] == archive_name),
            None,
        )
        archive_size = next(
            (archive["size_gb"] for archive in session.get("archives", []) if archive["archive_name"] == archive_name),
            None,
        )


        logging.info(f"Following is archive path {archive_path}")
        if archive_path:
            retrieve_path = os.path.join(archive_path)
            logging.info(f"Following is retrieval path {retrieve_path}")
            if os.path.exists(retrieve_path):
                already_retrieved.append(archive_name)
                logging.info(f"Testing.....")
            else:
                time.sleep(1)
                progress_log.append(f"Retrieving {archive_name} ...........")
                time.sleep(1)
                progress_log.append(f"⏳ Contacting Server........")
                try:
                    process = subprocess.Popen(
                        ["dsmc", "retrieve", "-server=ispsrv22", archive_path, retrieve_path],
                        stdin=slave,
                        stdout=slave,
                        stderr=slave,
                        text=True,
                        bufsize=0,
                        universal_newlines=True
                    )

                    os.close(slave)  # parent reads from master only

                    buffer = ""

                    while True:
                        try:
                            data = os.read(master, 1024).decode(errors="ignore")
                        except OSError:
                            break

                        if not data:
                            if process.poll() is not None:
                                break
                            continue

                        buffer += data

                        # process all <X GB> type tokens immediately even if no newline
                        matches = re.findall(r'<\s*([\d\.]+)\s*(GB|KB)\s*>', buffer)

                        for amount, unit in matches:
                            current_amount = float(amount)
                            delta = current_amount - last_reported_amount
                            logging.info(f"Delta = {delta}")
                            if delta > 0:
                                total_archive_size_retrieved += delta
                                logging.info(f"total archive size retrieved = {total_archive_size_retrieved}")
                                last_reported_amount = current_amount
                            percent_retrieved = calculate_retrieval_percentage(current_amount, archive_size)
                            total_percent_retrieved = calculate_retrieval_percentage(total_archive_size_retrieved, total_archive_size)
                            progress_log.append(
                                f"{amount} / {archive_size} {unit} Retrieved...\n"
                                f"Total: {round(total_archive_size_retrieved,2)} / {total_archive_size} {unit}\n"
                                f"Current: {percent_retrieved:.2f}%\n"
                                f"Overall: {total_percent_retrieved:.2f}%"
                            )
                            logging.info(f"{amount} {unit} Retrieved...")

                        # Remove processed tokens from buffer
                        buffer = re.sub(r'<\s*[\d\.]+\s*(GB|MB|KB)\s*>', '', buffer)

                    process.wait()
                    os.close(master)


                    # Check return code after loop
                    if process.returncode == 0:
                        successfully_retrieved.append(archive_name)
                        progress_log.append(f"✅ Retrieved {archive_name}")
                        retrieve_success = True
                        logging.info(f"Following archives retrieved successfully: {successfully_retrieved} on {retrieve_path} path")
                        time.sleep(1)
                    else:
                        # Capture stderr if it failed
                        stderr_output = process.stderr.read()
                        raise subprocess.CalledProcessError(process.returncode, process.args, output=stderr_output)
                        time.sleep(1)
                except subprocess.CalledProcessError as e:
                    logging.error(f"Error retrieving archive {archive_name}: {e}")
                    progress_log = []
                    progress_log.append(f"❌ Failed to retrieve {archive_name}: {e}")
                    time.sleep(1)

    # Extraction without custom path
    if extract_archive and  extract_path is None:
        logging.info("Inside extraction if without extract path")

        for archive_name in selected_archives:
            archive_path = next(
                (archive["file_path"] for archive in session.get("archives", []) if archive["archive_name"] == archive_name),
                None,
            )
            logging.info(f"Following is archive path {archive_path}")
            if archive_path and archive_path.endswith(".tar.gz"):
                progress_log.append(f"⏳ Extracting {archive_name}...")

                try:
                    with tarfile.open(archive_path, "r:gz") as tar:
                        archive_name = os.path.splitext(os.path.splitext(os.path.basename(archive_path))[0])[0]
                        logging.info(f"archive name: {archive_name}")
                        extract_to = os.path.dirname("/")

                        for member in tar.getmembers():
                            tar.extract(member, path=extract_to)

                    base_path = os.path.splitext(os.path.splitext(archive_path)[0])[0]  # removes .tar.gz
                    extracted_folder_path = os.path.join(os.path.dirname(archive_path), os.path.basename(base_path))
                    logging.info(f"following is the path where keep file must be created {extracted_folder_path}")
                    keep_file_path = os.path.join(extracted_folder_path, "KEEP")
                    open(keep_file_path, 'a').close()
                    time.sleep(1)
                    progress_log.append(f"✅ Extracted {archive_name}")
                    extraction_success.append(archive_name)
                    logging.info(f"Successfully extracted {archive_name} on {extracted_folder_path} path and created KEEP file")
                    extract_success = True
                    time.sleep(1)
                except (tarfile.TarError, OSError) as e:
                    progress_log.append(f"❌ Extraction failed for {archive_name}: {str(e)}")
                    extraction_errors.append(f"Failed to extract {archive_name}: {str(e)}")
                    logging.error(f"Error extracting {archive_name}: {e}")
                    time.sleep(1)
            else:
                extraction_errors.append(f"Invalid archive or path not found: {archive_name}")
        if removeArchive:
            logging.info("Starting removal after extraction (no custom path)")
            for archive_name in selected_archives:
                archive_path = next(
                    (archive["file_path"] for archive in session.get("archives", []) if archive["archive_name"] == archive_name),
                    None,
                )
                logging.info(f"Following is archive path {archive_path}")
                if archive_path:
                    retrieve_path = os.path.join(archive_path)
                    logging.info(f"Following is retrieval path {retrieve_path}")
                    if os.path.exists(retrieve_path):
                        progress_log.append(f"⏳ Removing {archive_name}...")
                        try:
                            os.remove(retrieve_path)
                            time.sleep(1)
                            progress_log.append(f"✅ Removed {archive_name}")
                            successfully_removed.append(archive_name)
                            remove_success1 = True
                            logging.info(f"Archives removed successfully")
                            archives = fetch_archives()
                            save_archives_to_file(archives)
                            session["archives"] = archives
                        except subprocess.CalledProcessError as e:
                            progress_log.append(f"❌ Removal failed for {archive_name}")
                            logging.error(f"Error removing archive {archive_name}: {e}")
                            failed_removals.append(archive_name)
                    else:
                        not_exist.append(archive_name)
                        logging.error(f"Removal Path does not exist")

    # Extraction with custom extract path
    elif extract_archive and extract_path is not None:
        logging.info("Inside extraction if with extract path")

        for archive_name in selected_archives:
            archive_path = next(
                (archive["file_path"] for archive in session.get("archives", []) if archive["archive_name"] == archive_name),
                None,
            )
            logging.info(f"Following is archive path {archive_path}")
            if archive_path and archive_path.endswith(".tar.gz"):
                progress_log.append(f"Extracting {archive_name}...")
                try:
                    with tarfile.open(archive_path, "r:gz") as tar:
                        archive_name = os.path.splitext(os.path.splitext(os.path.basename(archive_path))[0])[0]
                        logging.info(f"archive name: {archive_name}")
                        extract_to = os.path.dirname(extract_path)

                        for member in tar.getmembers():
                            parts = member.name.split('/')
                            indices = [i for i, p in enumerate(parts) if p == archive_name]

                            if len(indices) > 1:

                                # Use second occurrence only
                                use_idx = indices[0]
                                new_name = '/'.join(parts[use_idx:])
                                member.name = new_name
                                tar.extract(member, path=extract_to)
                            else:
                                # Only one or none â†’ leave original path intact
                                use_idx = indices[0]
                                new_name = '/'.join(parts[use_idx:])
                                member.name = new_name
                                tar.extract(member, path=extract_to)
                    base_path = os.path.splitext(os.path.splitext(archive_path)[0])[0]  # removes .tar.gz
                    extracted_folder_path = os.path.join(os.path.dirname(extract_path), os.path.basename(base_path))
                    logging.info(f"Testing")
                    logging.info(f"following is the path where keep file must be created {extracted_folder_path}")
                    keep_file_path = os.path.join(extracted_folder_path, "KEEP")
                    open(keep_file_path, 'a').close()
                    time.sleep(1)
                    progress_log.append(f" Extracted {archive_name}")
                    extraction_success.append(archive_name)
                    logging.info(f"Successfully extracted {archive_name} on {extracted_folder_path} path and created KEEP file")
                    extract_success = True
                    time.sleep(1)
                except (tarfile.TarError, OSError) as e:
                    progress_log.append(f" Extraction failed for {archive_name}: {str(e)}")
                    extraction_errors.append(f"Failed to extract {archive_name}: {str(e)}")
                    logging.error(f"Error extracting {archive_name}: {e}")
                    time.sleep(1)
            else:
                extraction_errors.append(f"Invalid archive or path not found: {archive_name}")
        if removeArchive:
            logging.info("Starting removal after extraction (with custom path)")
            for archive_name in selected_archives:
                archive_path = next(
                    (archive["file_path"] for archive in session.get("archives", []) if archive["archive_name"] == archive_name),
                    None,
                )
                logging.info(f"Following is archive path {archive_path}")
                if archive_path:
                    retrieve_path = os.path.join(archive_path)
                    logging.info(f"Following is retrieval path {retrieve_path}")
                    if os.path.exists(retrieve_path):
                        progress_log.append(f"â³ Removing {archive_name}...")
                        time.sleep(1)
                        try:
                            os.remove(retrieve_path)
                            progress_log.append(f"âœ… Removed {archive_name}")
                            time.sleep(1)
                            successfully_removed.append(archive_name)
                            remove_success1= True
                            logging.info("Archives removed successfully")
                            archives = fetch_archives()
                            save_archives_to_file(archives)
                            session["archives"] = archives
                        except subprocess.CalledProcessError as e:
                            progress_log.append(f"âŒ Removal failed for {archive_name}")
                            time.sleep(1)
                            logging.error(f"Error removing archive {archive_name}: {e}")
                            failed_removals.append(archive_name)
                    else:
                        not_exist.append(archive_name)
                        logging.error(f"Removal Path Does Not Exist")


    # default values in case force retrieve is not executed
    extraction_success1 = False
    successForceRetrieve = []
    successfully_removed1 = []
    force_retrieve_success = False


    if forceRetrieveArchives:
       logging.info(f"starting force retrieval")
       successForceRetrieve, extraction_success1, successfully_removed1, force_retrieve_success  = force_retrieve2(forceRetrieveArchives, extract_archive, removeArchive, extract_path)
       logging.info(f"following archive is force retrieved: {successForceRetrieve} ")

    messages = []

    if successfully_retrieved:  # if any archive has been retrieved
        retrieved_list = ", ".join(successfully_retrieved)
        messages.append(f"Successfully retrieved archives: {retrieved_list}")

    if extraction_success or extraction_success1:  # another boolean
        extracted_list = ", ".join(extraction_success )
        messages.append(f"extracted archives: {extracted_list}")

    if successfully_removed or successfully_removed1:
        removed_list = ", ".join(successfully_removed +  successfully_removed1)
        messages.append(f"Removed archives: {removed_list}")

    if successForceRetrieve:
        force_retrieved_list = ", ".join(successForceRetrieve)
        messages.append(f"Force-retrieved archives: {force_retrieved_list}")

    if messages:
        message = " | ".join(messages)
    else:
        message = "No actions performed."

    logging.info(f"{message} ")


    # Return JSON-safe response
    response_data = {
        "alreadyRetrieved": already_retrieved,
        "successfullyRetrieved": successfully_retrieved,
        "extractionSuccess": extraction_success,
        "extractionError": extraction_errors,
        "RemovalSuccess": successfully_removed,
        "forceRetrieveSuccess": successForceRetrieve,
        "SuccessMessage": message,
    }

    return jsonify(json.loads(json.dumps(response_data)))


@app.route("/retrieve-change-path", methods=["POST"])
def retrieve_change_path():
    """Retrieve selected archives using dsmc retrieve."""
    global progress_log
    progress_log = [] 

    data = request.get_json()
    selected_archives = data.get("archives", [])
    logging.info(f"Following are selected archives {selected_archives} ")
    forceRetrieveArchives = data.get("forceArchives", [])
    extract_archive = data.get("extract", False)
    removeArchive = data.get("remove", False)
    retrieve_path1 = data.get("retrieve_path")
    extract_path = data.get("extract_Path")
    already_retrieved = []
    successfully_retrieved = []
    extraction_success = []
    extraction_errors = []
    retrieve_success = False
    successfully_removed = []
    not_exist = []
    failed_removals = []
    extract_success1 = False
    remove_success1 = False

    total_archive_size = 0.0
    total_archive_size_retrieved = 0.0

    for archive_name in selected_archives:
        archive_size = next(
            (
                archive["size_gb"]
                for archive in session.get("archives", [])
                if archive["archive_name"] == archive_name
            ),
            None
        )

        if archive_size is not None:
            total_archive_size += size_to_mb(archive_size)



    for archive_name in selected_archives:
        master, slave = pty.openpty()
        last_reported_amount = 0.0

        archive_path = next(
            (archive["file_path"] for archive in session.get("archives", []) if archive["archive_name"] == archive_name),
            None,
        )

        archive_size = next(
            (archive["size_gb"] for archive in session.get("archives", []) if archive["archive_name"] == archive_name),
            None,
        )


        logging.info(f"Following is archive path  {archive_path} ")
        if archive_path:
            retrieve_path = os.path.join(retrieve_path1, archive_name)
            logging.info(f"Following is retrieval path {retrieve_path} ")
            if os.path.exists(retrieve_path):
                already_retrieved.append(archive_name)
            else:
                progress_log.append(f"⏳ Retrieving {archive_name}...")
                time.sleep(1)
                progress_log.append(f"â³ Contacting Server........")
                try:
                    process = subprocess.Popen(
                        ["dsmc", "retrieve", "-server=ispsrv22", archive_path, retrieve_path],
                        stdin=slave,
                        stdout=slave,
                        stderr=slave,
                        text=True,
                        bufsize=0,
                        universal_newlines=True

                    )
                    os.close(slave)  # parent reads from master only

                    buffer = ""

                    while True:
                        try:
                            data = os.read(master, 1024).decode(errors="ignore")
                        except OSError:
                            break

                        if not data:
                            if process.poll() is not None:
                                break
                            continue

                        buffer += data

                        # process all <X GB> type tokens immediately even if no newline
                        matches = re.findall(r'<\s*([\d\.]+)\s*(GB|KB)\s*>', buffer)

                        for amount, unit in matches:
                            current_amount = float(amount)
                            delta = current_amount - last_reported_amount
                            logging.info(f"Delta = {delta}")
                            if delta > 0:
                                total_archive_size_retrieved += delta
                                logging.info(f"total archive size retrieved = {total_archive_size_retrieved}")
                                last_reported_amount = current_amount
                            percent_retrieved = calculate_retrieval_percentage(current_amount, archive_size)
                            total_percent_retrieved = calculate_retrieval_percentage(total_archive_size_retrieved, total_archive_size)
                            progress_log.append(
                                f"{amount} / {archive_size} {unit} Retrieved...\n"
                                f"Total: {round(total_archive_size_retrieved,2)} / {total_archive_size} {unit}\n"
                                f"Current: {percent_retrieved:.2f}%\n"
                                f"Overall: {total_percent_retrieved:.2f}%"
                            )
                            logging.info(f"{amount} {unit} Retrieved...")

                        # Remove processed tokens from buffer
                        buffer = re.sub(r'<\s*[\d\.]+\s*(GB|MB|KB)\s*>', '', buffer)

                    process.wait()
                    os.close(master)


                    # Check return code after loop
                    if process.returncode == 0:
                        successfully_retrieved.append(archive_name)
                        progress_log.append(f"âœ… Retrieved {archive_name}")
                        retrieve_success = True
                        logging.info(f"Following archives retrieved successfully: {successfully_retrieved} on {retrieve_path} path")
                        time.sleep(1)
                    else:
                        # Capture stderr if it failed
                        stderr_output = process.stderr.read()
                        raise subprocess.CalledProcessError(process.returncode, process.args, output=stderr_output)
                        time.sleep(1)

                except subprocess.CalledProcessError as e:
                    logging.error(f"Error retrieving archive {archive_name}: {e}")
                    progress_log.append(f"❌ Failed to retrieve {archive_name}: {e}")
                    time.sleep(1)

    # Extraction without custom path
    if extract_archive and extract_path is None:
        logging.info("Inside extraction if without extract path")
        for archive_name in selected_archives:
            archive_path = next(
                (archive["file_path"] for archive in session.get("archives", []) if archive["archive_name"] == archive_name),
                None,
            )
            logging.info(f"Following is archive path {archive_path}")
            if archive_path and archive_path.endswith(".tar.gz"):
                progress_log.append(f"â³ Extracting {archive_name}...")

                try:
                    with tarfile.open(archive_path, "r:gz") as tar:
                        archive_name = os.path.splitext(os.path.splitext(os.path.basename(archive_path))[0])[0]
                        logging.info(f"archive name: {archive_name}")
                        extract_to = os.path.dirname("/")

                        for member in tar.getmembers():
                            tar.extract(member, path=extract_to)

                    base_path = os.path.splitext(os.path.splitext(archive_path)[0])[0]  # removes .tar.gz
                    extracted_folder_path = os.path.join(os.path.dirname(archive_path), os.path.basename(base_path))
                    logging.info(f"following is the path where keep file must be created {extracted_folder_path}")
                    keep_file_path = os.path.join(extracted_folder_path, "KEEP")
                    open(keep_file_path, 'a').close()
                    time.sleep(1)
                    progress_log.append(f"âœ… Extracted {archive_name}")
                    extraction_success.append(archive_name)
                    logging.info(f"Successfully extracted {archive_name} on {extracted_folder_path} path and created KEEP file")
                    extract_success = True
                    time.sleep(1)
                except (tarfile.TarError, OSError) as e:
                    progress_log.append(f"âŒ Extraction failed for {archive_name}: {str(e)}")
                    extraction_errors.append(f"Failed to extract {archive_name}: {str(e)}")
                    logging.error(f"Error extracting {archive_name}: {e}")
                    time.sleep(1)
            else:
                extraction_errors.append(f"Invalid archive or path not found: {archive_name}")

        if removeArchive:
            logging.info("Starting removal after extraction (no custom path)")
            for archive_name in selected_archives:
                archive_path = next(
                    (archive["file_path"] for archive in session.get("archives", []) if archive["archive_name"] == archive_name),
                    None,
                )
                logging.info(f"Following is archive path {archive_path}")
                if archive_path:
                    retrieve_path = os.path.join(archive_path)
                    logging.info(f"Following is retrieval path {retrieve_path}")
                    if os.path.exists(retrieve_path):
                        progress_log.append(f"⏳ Removing {archive_name}...")
                        time.sleep(1)
                        try:
                            os.remove(retrieve_path)
                            progress_log.append(f"✅ Removed {archive_name}")
                            time.sleep(1)
                            successfully_removed.append(archive_name)
                            remove_success1 = True
                            archives = fetch_archives()
                            save_archives_to_file(archives)
                            session["archives"] = archives
                        except subprocess.CalledProcessError as e:
                            logging.error(f"Error removing archive {archive_name}: {e}")
                            failed_removals.append(archive_name)
                            progress_log.append(f"❌ Removal failed for {archive_name}")
                            time.sleep(1)
                    else:
                        not_exist.append(archive_name)
                        logging.info(f"Removal Path does not exist")

    # Extraction with custom extract path
    elif extract_archive and extract_path is not None:
        logging.info("Inside extraction if with extract path")
        for archive_name in selected_archives:
            archive_path = next(
                (archive["file_path"] for archive in session.get("archives", []) if archive["archive_name"] == archive_name),
                None,
            )
            logging.info(f"Following is archive path {archive_path}")
            if archive_path and archive_path.endswith(".tar.gz"):
                try:
                    with tarfile.open(archive_path, "r:gz") as tar:
                        archive_name = os.path.splitext(os.path.splitext(os.path.basename(archive_path))[0])[0]
                        logging.info(f"archive name: {archive_name}")
                        extract_to = os.path.dirname(extract_path)

                        for member in tar.getmembers():
                            parts = member.name.split('/')
                            indices = [i for i, p in enumerate(parts) if p == archive_name]

                            if len(indices) > 1:

                                # Use second occurrence only
                                use_idx = indices[1]
                                new_name = '/'.join(parts[use_idx:])
                                member.name = new_name
                                logging.info(f"Trimmed (2nd occurrence): {member.name}")
                                tar.extract(member, path=extract_to)
                            else:
                                # Only one or none Ã¢â€ â€™ leave original path intact
                                logging.info(f"Skipping trim (only one or none): {member.name}")
                                use_idx = indices[0]
                                new_name = '/'.join(parts[use_idx:])
                                member.name = new_name
                                tar.extract(member, path=extract_to)

                    base_path = os.path.splitext(os.path.splitext(archive_path)[0])[0]  # removes .tar.gz
                    extracted_folder_path = os.path.join(os.path.dirname(extract_path), os.path.basename(base_path))
                    logging.info(f"Testing")
                    logging.info(f"following is the path where keep file must be created {extracted_folder_path}")
                    keep_file_path = os.path.join(extracted_folder_path, "KEEP")
                    open(keep_file_path, 'a').close()
                    time.sleep(1)
                    progress_log.append(f" Extracted {archive_name}")
                    extraction_success.append(archive_name)
                    logging.info(f"Successfully extracted {archive_name} on {extracted_folder_path} path and created KEEP file")
                    extract_success = True
                    time.sleep(1)
                except (tarfile.TarError, OSError) as e:
                    progress_log.append(f" Extraction failed for {archive_name}: {str(e)}")
                    extraction_errors.append(f"Failed to extract {archive_name}: {str(e)}")
                    logging.error(f"Error extracting {archive_name}: {e}")
                    time.sleep(1)
            else:
                extraction_errors.append(f"Invalid archive or path not found: {archive_name}")

        if removeArchive:
            logging.info("Starting removal after extraction (with custom path)")
            for archive_name in selected_archives:
                archive_path = next(
                    (archive["file_path"] for archive in session.get("archives", []) if archive["archive_name"] == archive_name),
                    None,
                )
                logging.info(f"Following is archive path {archive_path}")
                if archive_path:
                    retrieve_path = os.path.join(archive_path)
                    logging.info(f"Following is retrieval path {retrieve_path}")
                    if os.path.exists(retrieve_path):
                        progress_log.append(f"⏳ Removing {archive_name}...")
                        try:
                            os.remove(retrieve_path)
                            progress_log.append(f"✅ Removed {archive_name}")
                            successfully_removed.append(archive_name)
                            remove_success1 = True
                            archives = fetch_archives()
                            save_archives_to_file(archives)
                            session["archives"] = archives
                        except subprocess.CalledProcessError as e:
                            logging.error(f"Error removing archive {archive_name}: {e}")
                            failed_removals.append(archive_name)
                            progress_log.append(f"❌ Removal failed for {archive_name}")
                    else:
                        not_exist.append(archive_name)
                        logging.info(f"Removal Path Does Not Exist")

    if forceRetrieveArchives is not None:
        logging.info(f"starting force retrieval")
        successForceRetrieve, extraction_success1, successfully_removed1, force_retrieve_success  = force_retrieve2(forceRetrieveArchives, extract_archive, removeArchive, extract_path)
        logging.info(f"following archive is force retrieved: {successForceRetrieve} ")

    messages = []

    if successfully_retrieved:  # if any archive has been retrieved
        retrieved_list = ", ".join(successfully_retrieved)
        messages.append("Successfully retrieved archives: {retrieved_list}")

    if extraction_success or extraction_success1:  # another boolean
        extracted_list = ", ".join(extraction_success +  extraction_success1)
        messages.append("extracted archives: : {extracted_list}")

    if successfully_removed or successfully_removed1:
        removed_list = ", ".join(successfully_removed +  successfully_removed1)
        messages.append(f"Removed archives: {removed_list}")

    if successForceRetrieve:
        force_retrieved_list = ", ".join(successForceRetrieve)
        messages.append(f"Force-retrieved archives: {force_retrieved_list}")

    if messages:
        message = " | ".join(messages)
    else:
        message = "No actions performed."

    logging.info(f"{message} ")


    # Ã¢Å“â€¦ Ensure JSON-safe formatting
    response_data = {
        "alreadyRetrieved": already_retrieved,
        "successfullyRetrieved": successfully_retrieved,
        "extractionSuccess": extraction_success,
        "extractionError": extraction_errors,
        "SuccessMessage": message,
    }

    return jsonify(json.loads(json.dumps(response_data)))  # Safe JSON response

@app.route("/extract", methods=["POST"])
def extract_archives():
    """Extract selected archives using tar -xzf."""
    data = request.get_json()
    selected_archives = data.get("archives", [])

    print(f"Selected archives for extraction: {selected_archives}")

    extraction_success = []
    extraction_errors = []

    for archive_name in selected_archives:
        archive_path = next(
            (archive["file_path"] for archive in session.get("archives", []) if archive["archive_name"] == archive_name),
            None,
        )

        print(f"Following is archive path  {archive_path} ")
        print(f"Following is archive name  {archive_name} ")
        extraction_path = "/var/www/html/public/"
        if archive_path.endswith(".tar.gz"):
            extract_command = ["tar", "-xzf", archive_path, "-C", extraction_path]

            try:
                subprocess.run(
                    extract_command,
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                extraction_success.append(archive_name)
                print(f"Successfully extracted {archive_name}")
            except subprocess.CalledProcessError as e:
                extraction_errors.append(f"Failed to extract {archive_name}: {str(e)}")
                print(f"Error extracting {archive_name}: {e}")
        else:
            extraction_errors.append(f"Invalid archive or path not found: {archive_name}")

    # JSON response including extraction results
    response_data = {
        "extractionSuccess": extraction_success,
        "extractionErrors": extraction_errors
    }

    return jsonify(response_data)

@app.route("/force-retrieve", methods=["POST"])
def force_retrieve():
    """Force retrieve selected archives by automating user action input."""
    global progress_log
    progress_log = []
    data = request.get_json()
    selected_archives = data.get("archives", [])

    force_retrieved = []
    failed_retrievals = []

    for archive_name in selected_archives:
        archive_path = next(
            (archive["file_path"] for archive in session.get("archives", []) if archive["archive_name"] == archive_name),
            None,
        )

        if archive_path:
            retrieve_path = os.path.join(archive_path)

            try:
                # Ã¢Å“â€¦ Automating user input for force retrieval (choosing option 1 "Replace this object")
                process = subprocess.Popen(
                    ["dsmc", "retrieve", "-server=ispsrv22", archive_path, retrieve_path],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )

                # Ã¢Å“â€¦ Sending "1\n" as user input to choose "Replace
                stdout, stderr = process.communicate(input="1\n")

                if process.returncode == 0:
                    force_retrieved.append(archive_name)
                else:
                    failed_retrievals.append({"archive": archive_name, "error": stderr})

            except subprocess.CalledProcessError as e:
                print(f"Error force retrieving archive {archive_name}: {e}")
                failed_retrievals.append({"archive": archive_name, "error": str(e)})



    force_response_data = {
          "forceRetrieved": force_retrieved,

    }

    return jsonify(json.loads(json.dumps(force_response_data)))  # Safe JSON response

@app.route("/force-retrieve", methods=["POST"])
def force_retrieve2(selectedForceRetrieve, extract_archive, removeArchive, extract_path):
    """Force retrieve selected archives by automating user action input."""
    global progress_log
    progress_log = []
    force_retrieved = []
    failed_retrievals = []
    extraction_success = []
    extraction_errors = []
    successfully_removed = []
    not_exist = []
    failed_removals = []
    extract_success = False
    remove_success = False
    force_retrieve_success = False
    successfully_retrieved = []

    total_archive_size = 0.0
    total_archive_size_retrieved = 0.0

    for archive_name in selectedForceRetrieve:
        archive_size = next(
            (
                archive["size_gb"]
                for archive in session.get("archives", [])
                if archive["archive_name"] == archive_name
            ),
            None
        )

        if archive_size is not None:
            total_archive_size += size_to_mb(archive_size)


    for archive_name in selectedForceRetrieve:
        master, slave = pty.openpty()
        last_reported_amount = 0.0
        archive_path = next(
            (archive["file_path"] for archive in session.get("archives", []) if archive["archive_name"] == archive_name),
            None,
        )
        archive_size = next(
            (archive["size_gb"] for archive in session.get("archives", []) if archive["archive_name"] == archive_name),
            None,
        )

        if archive_path:
            retrieve_path = os.path.join(archive_path)
            logging.info(f"Following is retrieve path {retrieve_path}")
            progress_log.append(f"Ã¢Â³ Force Retrieving {archive_name}...")
            if os.path.exists(retrieve_path):
                progress_log = []
                progress_log.append(f"Ã¢Â³ Removing {archive_name}...")
                try:
                    os.remove(retrieve_path)
                    progress_log = []
                    progress_log.append(f"Ã¢Å“â€¦ Removed {archive_name}")
                    successfully_removed.append(archive_name)
                    remove_success = True
                    archives = fetch_archives()
                    save_archives_to_file(archives)
                    session["archives"] = archives
                except subprocess.CalledProcessError as e:
                    progress_log = []
                    progress_log.append(f"Ã¢Å’ Removal failed for {archive_name}")
                    logging.error(f"Error removing archive {archive_name}: {e}")
                    failed_removals.append(archive_name)
            else:
                not_exist.append(archive_name)
                logging.error(f"Removal Path Does Not Exist")
            time.sleep(1)
            progress_log.append(f" Contacting Server........")
            try:
                process = subprocess.Popen(
                    ["dsmc", "retrieve", "-server=ispsrv22", archive_path, retrieve_path],
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    text=True,
                    bufsize=0,
                    universal_newlines=True

                )
                os.close(slave)  # parent reads from master only

                buffer = ""

                while True:
                    try:
                        data = os.read(master, 1024).decode(errors="ignore")
                    except OSError:
                        break

                    if not data:
                        if process.poll() is not None:
                            break
                        continue
                    buffer += data

                    # process all <X GB> type tokens immediately even if no newline
                    matches = re.findall(r'<\s*([\d\.]+)\s*(GB|KB)\s*>', buffer)

                    for amount, unit in matches:
                        current_amount = float(amount)
                        delta = current_amount - last_reported_amount
                        logging.info(f"Delta = {delta}")
                        if delta > 0:
                            total_archive_size_retrieved += delta
                            logging.info(f"total archive size retrieved = {total_archive_size_retrieved}")
                            last_reported_amount = current_amount
                        percent_retrieved = calculate_retrieval_percentage(current_amount, archive_size)
                        total_percent_retrieved = calculate_retrieval_percentage(total_archive_size_retrieved, total_archive_size)
                        progress_log.append(
                            f"{amount} / {archive_size} {unit} Retrieved...\n"
                            f"Total: {round(total_archive_size_retrieved,2)} / {total_archive_size} {unit}\n"
                            f"Current: {percent_retrieved:.2f}%\n"
                            f"Overall: {total_percent_retrieved:.2f}%"
                        )
                        logging.info(f"{amount} {unit} Retrieved...")

                    # Remove processed tokens from buffer
                    buffer = re.sub(r'<\s*[\d\.]+\s*(GB|MB|KB)\s*>', '', buffer)

                process.wait()
                os.close(master)


                # Check return code after loop
                if process.returncode == 0:
                    force_retrieved.append(archive_name)
                    progress_log.append(f"ÃƒÂ¢Ã…â€œÃ¢â‚¬Â¦ Retrieved {archive_name}")
                    force_retrieve_success = True
                    logging.info(f"Following archives retrieved successfully: {successfully_retrieved} on {retrieve_path} path")
                    time.sleep(1)
                else:
                    # Capture stderr if it failed
                    stderr_output = process.stderr.read()
                    raise subprocess.CalledProcessError(process.returncode, process.args, output=stderr_output)
                    time.sleep(1)

            except subprocess.CalledProcessError as e:
                logging.error(f"Error retrieving archive {archive_name}: {e}")
                progress_log.append(f"Ã¢Å’ Failed to retrieve {archive_name}: {e}")
                time.sleep(1)

    # Extraction without custom path
    if extract_archive and extract_path is None:
        logging.info(f"Inside extraction if without extract path")

        for archive_name in selectedForceRetrieve:
            archive_path = next(
                (archive["file_path"] for archive in session.get("archives", []) if archive["archive_name"] == archive_name),
                None,
            )
            logging.info(f"Following is archive path {archive_path}")
            logging.info(f"Current working directory: {os.getcwd()}")
            if archive_path and archive_path.endswith(".tar.gz"):
                progress_log.append(f"ÃƒÂ¢Ã‚Â³ Extracting {archive_name}...")

                try:
                    with tarfile.open(archive_path, "r:gz") as tar:
                        archive_name = os.path.splitext(os.path.splitext(os.path.basename(archive_path))[0])[0]
                        logging.info(f"archive name: {archive_name}")
                        extract_to = os.path.dirname("/")

                        for member in tar.getmembers():
                            tar.extract(member, path=extract_to)

                    base_path = os.path.splitext(os.path.splitext(archive_path)[0])[0]  # removes .tar.gz
                    extracted_folder_path = os.path.join(os.path.dirname(archive_path), os.path.basename(base_path))
                    logging.info(f"following is the path where keep file must be created {extracted_folder_path}")
                    keep_file_path = os.path.join(extracted_folder_path, "KEEP")
                    open(keep_file_path, 'a').close()
                    time.sleep(1)
                    progress_log.append(f"ÃƒÂ¢Ã…â€œÃ¢â‚¬Â¦ Extracted {archive_name}")
                    extraction_success.append(archive_name)
                    logging.info(f"Successfully extracted {archive_name} on {extracted_folder_path} path and created KEEP file")
                    extract_success = True
                    time.sleep(1)
                except (tarfile.TarError, OSError) as e:
                    progress_log.append(f"ÃƒÂ¢Ã…â€™ Extraction failed for {archive_name}: {str(e)}")
                    extraction_errors.append(f"Failed to extract {archive_name}: {str(e)}")
                    logging.error(f"Error extracting {archive_name}: {e}")
                    time.sleep(1)
            else:
                extraction_errors.append(f"Invalid archive or path not found: {archive_name}")

        if removeArchive:
            logging.info("Starting removal after extraction (no custom path)")
            for archive_name in selectedForceRetrieve:
                archive_path = next(
                    (archive["file_path"] for archive in session.get("archives", []) if archive["archive_name"] == archive_name),
                    None,
                )
                logging.info(f"Following is archive path {archive_path}")
                if archive_path:
                    retrieve_path = os.path.join(archive_path)
                    logging.info(f"Following is retrieval path {retrieve_path}")
                    if os.path.exists(retrieve_path):
                        progress_log.append(f"Ã¢Â³ Removing {archive_name}...")
                        try:
                            os.remove(retrieve_path)
                            progress_log = []
                            progress_log.append(f"Ã¢Å“â€¦ Removed {archive_name}")
                            successfully_removed.append(archive_name)
                            remove_success = True
                            archives = fetch_archives()
                            save_archives_to_file(archives)
                            session["archives"] = archives
                        except subprocess.CalledProcessError as e:
                            progress_log = []
                            progress_log.append(f"Ã¢Å’ Removal failed for {archive_name}")
                            logging.error(f"Error removing archive {archive_name}: {e}")
                            failed_removals.append(archive_name)
                    else:
                        not_exist.append(archive_name)
                        logging.error(f"Removal Path does not exist")

    # Extraction with custom extract path
    elif extract_archive and extract_path is not None:
        logging.info("Inside extraction if with extract path")
        for archive_name in selectedForceRetrieve:
            archive_path = next(
                (archive["file_path"] for archive in session.get("archives", []) if archive["archive_name"] == archive_name),
                None,
            )
            print(f"Following is archive path {archive_path}")
            if archive_path and archive_path.endswith(".tar.gz"):
                progress_log.append(f"Extracting {archive_name}...")
                try:
                    with tarfile.open(archive_path, "r:gz") as tar:
                        archive_name = os.path.splitext(os.path.splitext(os.path.basename(archive_path))[0])[0]
                        logging.info(f"archive name: {archive_name}")
                        extract_to = os.path.dirname(extract_path)

                        for member in tar.getmembers():
                            parts = member.name.split('/')
                            indices = [i for i, p in enumerate(parts) if p == archive_name]

                            if len(indices) > 1:

                                # Use second occurrence only
                                use_idx = indices[1]
                                new_name = '/'.join(parts[use_idx:])
                                member.name = new_name
                                logging.info(f"Trimmed (2nd occurrence): {member.name}")
                                tar.extract(member, path=extract_to)
                            else:
                                # Only one or none ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬ ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ leave original path intact
                                logging.info(f"Skipping trim (only one or none): {member.name}")
                                use_idx = indices[0]
                                new_name = '/'.join(parts[use_idx:])
                                member.name = new_name
                                tar.extract(member, path=extract_to)

                    base_path = os.path.splitext(os.path.splitext(archive_path)[0])[0]  # removes .tar.gz
                    extracted_folder_path = os.path.join(os.path.dirname(extract_path), os.path.basename(base_path))
                    logging.info(f"Testing")
                    logging.info(f"following is the path where keep file must be created {extracted_folder_path}")
                    keep_file_path = os.path.join(extracted_folder_path, "KEEP")
                    open(keep_file_path, 'a').close()
                    time.sleep(1)
                    progress_log.append(f" Extracted {archive_name}")
                    extraction_success.append(archive_name)
                    logging.info(f"Successfully extracted {archive_name} on {extracted_folder_path} path and created KEEP file")
                    extract_success = True
                    time.sleep(1)
                except (tarfile.TarError, OSError) as e:
                    progress_log.append(f" Extraction failed for {archive_name}: {str(e)}")
                    extraction_errors.append(f"Failed to extract {archive_name}: {str(e)}")
                    logging.error(f"Error extracting {archive_name}: {e}")
                    time.sleep(1)
            else:
                extraction_errors.append(f"Invalid archive or path not found: {archive_name}")



        if removeArchive:
            logging.info("Starting removal after extraction (with custom path)")
            for archive_name in selectedForceRetrieve:
                archive_path = next(
                    (archive["file_path"] for archive in session.get("archives", []) if archive["archive_name"] == archive_name),
                    None,
                )
                logging.info(f"Following is archive path {archive_path}")
                if archive_path:
                    retrieve_path = os.path.join(archive_path)
                    logging.info(f"Following is retrieval path {retrieve_path}")
                    if os.path.exists(retrieve_path):
                        progress_log = []
                        progress_log.append(f"Ã¢Â³ Removing {archive_name}...")
                        try:
                            os.remove(retrieve_path)
                            progress_log = []
                            progress_log.append(f"Ã¢Å“â€¦ Removed {archive_name}")
                            successfully_removed.append(archive_name)
                            remove_success = True
                            archives = fetch_archives()
                            save_archives_to_file(archives)
                            session["archives"] = archives
                        except subprocess.CalledProcessError as e:
                            progress_log = []
                            progress_log.append(f"Ã¢Å’ Removal failed for {archive_name}")
                            logging.error(f"Error removing archive {archive_name}: {e}")
                            failed_removals.append(archive_name)
                    else:
                        not_exist.append(archive_name)
                        logging.error(f"Removal Path Does Not Exist")


    return force_retrieved, extraction_success, successfully_removed, force_retrieve_success   # Safe JSON response


@app.route("/remove-archives", methods=["POST"])
def remove_Archives():
    """Force retrieve selected archives by automating user action input."""
    global progress_log
    progress_log = []
    data = request.get_json()
    selected_archives = data.get("archives", [])

    Removed = []
    failed_removals = []
    failed_removals_error = []

    for archive_name in selected_archives:
        archive_path = next(
            (archive["file_path"] for archive in session.get("archives", []) if archive["archive_name"] == archive_name),
            None,
        )
        print(f"Following is archive path  {archive_path} ")
        if archive_path:
            retrieve_path = os.path.join("/var/www/html/public/", archive_name)

            progress_log.append(f"â³ Removing {archive_name}...")
            try:
                # ÃƒÆ’Ã‚Â¢Ãƒâ€¦Ã¢â‚¬Å“ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ Automating user input for force retrieval (choosing option 1 "Replace this object")
                process = subprocess.run(
                    ["dsmc", "del", "archive", "-server=ispsrv22", archive_path],
                    check=True,
                    input="Y\n",
                    text=True,
                    capture_output=True
                )

                progress_log.append(f"âœ… Removed {archive_name}")
                Removed.append(archive_name)
            except subprocess.CalledProcessError as e:
                print(f"Error removing  archive {archive_name}: {e}")
                failed_removals.append(archive_name)
                failed_removals_error.append(e.stderr)
                progress_log.append(f"âŒ Failed to remove {archive_name}: {e}")

    # Refresh archives ONLY ONCE after all removals
    archives = fetch_archives()
    save_archives_to_file(archives)
    session["archives"] = archives

    force_response_data = {
          "Removed": Removed,
          "failedRemovals": failed_removals,
          "failedRemovalsError": failed_removals_error
    }
    
    return jsonify(json.loads(json.dumps(force_response_data)))  # Safe JSON response

@app.route("/remove-local", methods=["POST"])
def remove_Archives_Local():
    """Retrieve selected archives using dsmc retrieve."""
    data = request.get_json()
    selected_archives = data.get("archives", [])
    print(f"Following are selected archives {selected_archives} ")
    extract_archive = data.get("extract", False)

    not_exist = []
    successfully_removed = []
    failed_removals = []

    for archive_name in selected_archives:
        archive_path = next(
            (archive["file_path"] for archive in session.get("archives", []) if archive["archive_name"] == archive_name),
            None,
        )
        print(f"Following is archive path  {archive_path} ")
        if archive_path:
            retrieve_path = os.path.join("/var/www/html/public/test/", archive_name)
            print(f"Following is retrieval path {retrieve_path} ")
            if os.path.exists(retrieve_path):
                try:
                    process = subprocess.run(
                        ["dsmc", "del", "archive", "-server=ispsrv22", retrieve_path],
                        check=True,
                        input="Y\n",
                        text=True,
                        capture_output=True
                    )

                    successfully_removed.append(archive_name)
                    archives = fetch_archives()
                    save_archives_to_file(archives)
                    session["archives"] = archives
                except subprocess.CalledProcessError as e:
                    print(f"Error removing  archive {archive_name}: {e}")
                    failed_removals.append(archive_name)
                
            else:
                not_exist.append(archive_name)

    # ÃƒÂ¢Ã…â€œÃ¢â‚¬Â¦ Ensure JSON-safe formatting
    response_data = {
        "notExist": not_exist,
        "successfullyRemoved": successfully_removed,
        "failedRemoval": failed_removals
    }
    
    return jsonify(json.loads(json.dumps(response_data)))

@app.route("/progress")
def get_progress():
    return jsonify(progress_log)

@app.route("/check-path", methods=["POST"])
def check_path():
    data = request.get_json()
    path = data.get("path")
    return jsonify({"exists": os.path.exists(path)})

@app.route("/create-path", methods=["POST"])
def create_path():
    logging.info(f"Creating file path")
    data = request.get_json()
    path = data.get("path")
    logging.info(f"Following is the path to create: { path }")

    try:
        os.makedirs(path, exist_ok=True)
        logging.info(f"Path created successfully")
        return jsonify({"success": True})
    except Exception as e:
        logging.error(f"Error occured in creating path: {str(e) }")
        return jsonify({"success": False, "error": str(e)})

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST" and request.form.get("action") == "fetch_archives":
        archives = fetch_archives()
        save_archives_to_file(archives)
        session["archives"] = archives
    elif request.method == "POST" and request.form.get("action") == "fetch_last_saved":
        archives = load_archives_from_last_file()
        session["archives"] = archives

    archives = session.get("archives", [])

    return render_template_string(
        """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Archive Browser</title>
            <link href="https://cdn.jsdelivr.net/npm/bulma@0.9.3/css/bulma.min.css" rel="stylesheet"> 
            <link href="https://cdn.datatables.net/1.13.1/css/jquery.dataTables.min.css" rel="stylesheet">
            <script src="https://code.jquery.com/jquery-3.6.0.min.js"></script>
            <script src="https://cdn.datatables.net/1.13.1/js/jquery.dataTables.min.js"></script>

            <!-- Bootstrap CSS -->
            <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
            <!-- Bootstrap JS -->
            <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>


        </head>
        <body>
<!-- Bootstrap Modal for Messages -->
<div class="modal" id="messageModal" tabindex="-1" aria-labelledby="messageModalLabel" aria-hidden="true">
  <div class="modal-dialog">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title" id="messageModalLabel">Message</h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
      </div>
      <div class="modal-body" id="messageModalBody">
        <!-- Message content will be inserted here -->
      </div>
      <div class="modal-footer" id="messageModalFooter">
        <!-- Buttons will be dynamically inserted here -->
      </div>
    </div>
  </div>
</div>

<div class="modal fade" id="confirmationModal" tabindex="-1" aria-labelledby="confirmationModal1Label" aria-hidden="true">
        <div class="modal-dialog">
            <div class="modal-content">
                <div class="modal-header">
                    <h5 class="modal-title" id="modalLabel">Archive will be removed permanently. Please type "Yes/No" to confirm.</h5>
                    <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
                </div>
                <div class="modal-body">
                    <input type="text" class="form-control" id="modalInput" placeholder="Yes/No">
                </div>
                <div class="modal-footer">
                    <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
                    <button type="button" class="btn btn-primary" id="okButton1">OK</button>
                </div>
            </div>
        </div>
    </div>


<div class="modal fade" id="confirmationModal2" tabindex="-1" aria-labelledby="confirmationModal2Label" aria-hidden="true">
        <div class="modal-dialog">
            <div class="modal-content">
                <div class="modal-header">
                    <h5 class="modal-title" id="modalLabel">Following archives will be removed permanently. Press OK to continue.</h5>
                    <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
                </div>
                <div class="modal-body" id="confirmationModalBody">
                    
                </div>
                <div class="modal-footer">
                    <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
                    <button type="button" class="btn btn-primary" id="okButton2">OK</button>
                </div>
            </div>
        </div>
    </div>

<div class="modal fade" id="inputModal" tabindex="-1" aria-labelledby="inputmodalLabel" aria-hidden="true">
        <div class="modal-dialog">
            <div class="modal-content">
                <div class="modal-header">
                    <h5 class="modal-title" id="modalLabel">Enter Path</h5>
                    <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
                </div>
                <div class="modal-body">
                    <input type="text" class="form-control" id="modalInput" placeholder="/Enter/new/file/path/">
                </div>
                <div class="modal-footer">
                    <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
                    <button type="button" class="btn btn-primary" id="okButton">OK</button>
                </div>
            </div>
        </div>
    </div>

            <section class="section">
                <div class="container">
                    <h1 class="title">Archive Browser</h1>
                    <form method="POST">
                        <button name="action" value="fetch_archives" class="button is-primary">Fetch Archives</button>
                        <button name="action" value="fetch_last_saved" class="button is-info">Fetch Archives from Last Saved File</button>
                    </form>
<label for="sizeUnit">Select Size Unit:</label>
<select id="sizeUnit" class="select">
    <option value="gigabytes">Gigabytes</option>
    <option value="megabytes">MegaBytes</option>
    <option value="kilobytes">Kilobytes</option>
    <option value="dynamic">Dynamic</option>
    <option value="original">Original</option>
</select>
                    <table id="archiveTable" class="table is-fullwidth is-striped display">
                        <thead>
                            <tr>
                                <th>Size</th>
                                <th>Archive Date</th>
                                <th>Time</th>
                                <th>File Path</th>
                                <th>Archive Name</th>
                                <th>Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for archive in archives %}
                            <tr>
                                <td class="size-cell"
                data-gigabytes="{{ archive.size_gb }}"
                data-megabytes="{{ archive.size_mb }}"
                data-kilobytes="{{ archive.size_kb }}"
                data-original="{{ archive.size }}">
                {{ archive.size_gb }}
            </td>
                                <td>{{ archive.archive_date }}</td>
                                <td>{{ archive.time }}</td>
                                <td>{{ archive.file_path }}</td>
                                <td>{{ archive.archive_name }}</td>
                                <td><input type="checkbox" class="archive-checkbox" value="{{ archive.archive_name }}"></td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                    <form>
<div id="selectedCount" class="mt-2">
    Selected archives: 0
</div>
<div id="selectedTotalSize" class="mt-2">
    Total Size: 0
</div>


</form>

                    <button id="toggle-filter" class="button is-info">Filter</button>
                    <button id="retrieve-selected" class="button is-success">Retrieve Selected</button>
                    <button id="remove-selected" class="btn btn-danger">Remove Archives</button>
                </div>
<div id="loader" style="display: none; text-align: center;">
    <div class="spinner"></div>
    <p id="loader-label" style="margin-top: 10px; color: #0dcaf0; font-weight: bold;">please wait for a moment...</p>

</div>

<!-- Progress Bar Container -->
<div id="progressBarContainer">
  <div id="progressBar"></div>
  <div id="progressText">Preparing...</div>
</div>

<!-- Modal -->
<!-- Bootstrap Modal for Archive Retrieval -->
<div class="modal fade" id="archiveModal" tabindex="-1" aria-labelledby="archiveModalLabel" aria-hidden="true">
  <div class="modal-dialog modal-lg"> <!-- Wider modal -->
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title" id="archiveModalLabel">Confirm Archive Action</h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
      </div>

      <div class="modal-body">
        <!-- Archive List -->
        <div id="selectedArchivesList" class="mb-3">
          <!-- JS will insert archive rows here -->
        </div>

        <hr>

        <!-- Global Options -->
        <div class="mb-3">
          <label>
            <input type="checkbox" id="extractCheckbox" />
            <strong>Extract Archives</strong>
          </label>
          <input type="text" id="extractPath" class="form-control mt-2" placeholder="Extract path (optional)" disabled />
          <div id="extractPathError" class="error-message"></div>
        </div>

        <div class="mb-3">
          <label>
            <input type="checkbox" id="removeCheckbox" disabled />
            <strong>Remove Archives</strong>
          </label>
        </div>

        <div class="mb-3">
          <label for="customRetrievePath"><strong>Retrieve To Path:</strong></label>
          <input type="text" id="customRetrievePath" class="form-control mt-2" placeholder="/your/custom/path/" />
          <div id="retrievePathError" class="error-message"></div>
        </div>
      </div>

      <div class="modal-footer">
        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
        <button id="confirm-retrieve" type="button" class="btn btn-primary">Confirm</button>
      </div>
    </div>
  </div>
</div>


<style>
    #loader {
        position: fixed;
        top: 0;
        left: 0;
        width: 100%;
        height: 100%;
        background: rgba(0, 0, 0, 0.5);
        display: flex;
        align-items: center;
        justify-content: center;
        z-index: 1000;
    }

    .spinner {
        width: 50px;
        height: 50px;
        border: 5px solid rgba(255, 255, 255, 0.3);
        border-top: 5px solid #fff;
        border-radius: 50%;
        animation: spin 1s linear infinite;
    }

    @keyframes spin {
        0% { transform: rotate(0deg); }
        100% { transform: rotate(360deg); }
    }
.modal-content {
  background-color: #fff;
  padding: 20px;
  border-radius: 12px;
  margin-top: 100px;
  box-shadow: 0px 0px 10px #000;
}

.archive-row {
  padding: 8px 0;
  border-bottom: 1px solid #eee;
}
.archive-name {
  font-weight: bold;
}
.modal-backdrop {
    z-index: 1040 !important;
}
.modal {
    z-index: 1050 !important;
}

.error-message {
    color: red;
    font-size: 12px;
    margin-top: 4px;
    display: none; /* hidden by default */
}


/* Progress bar container */
#progressBarContainer {
  position: fixed;
  top: 0;  /* use bottom: 0; if you prefer it at bottom */
  left: 0;
  width: 100%;
  height: auto; /* allow it to grow with messages */
  background-color: #222;
  color: #00ff66;
  font-weight: bold;
  font-size: 18px;
  display: none; /* hidden by default */
  justify-content: flex-start;
  align-items: flex-start;
  text-align: left;
  padding: 10px;
  z-index: 9999;
}

/* The green bar that fills up */
#progressBar {
  position: absolute;
  top: 0;
  left: 0;
  height: 4px;
  background-color: #00ff66;
  width: 0%;
  transition: width 0.3s ease;
}

/* Text shown over the bar */
#progressText {
  text-align: left;
  white-space: pre-line;
}


</style>
            </section>
<script>


let alreadyRetrievedArchives = [];
let selectedRows = [];

function updateSelectedCount(archives) {
    document.getElementById("selectedCount").innerText =
        `Selected archives: ${archives.size}`;
}

function parseSize(sizeString) {

    if (!sizeString) {
        return null;
    }

    sizeString = sizeString.toString().trim();

    const parts = sizeString.split(/\s+/);

    if (parts.length < 2) {
        return null;
    }

    // Remove thousands separators
    const value = parseFloat(parts[0].replace(/,/g, ""));

    const unit = parts[1].toUpperCase();

    if (isNaN(value)) {
        return null;
    }

    const multipliers = {
        "B": 1,
        "KB": 1024,
        "MB": 1024 ** 2,
        "GB": 1024 ** 3,
        "TB": 1024 ** 4,
        "PB": 1024 ** 5
    };

    if (!(unit in multipliers)) {
        return null;
    }

    return {
        value: value,
        unit: unit,
        bytes: value * multipliers[unit]
    };
}

function updateSelectedTotalSize() {

    let sizes = [];
    selectedRows.forEach(function(row) {

        console.log("Archive:", row.archiveName);
        console.log("Original size:", row.size);

        const parsed = parseSize(row.size);

        console.log("Parsed:", parsed);

        if (parsed) {
            sizes.push(parsed);
        }
    });

    console.log("Final Sizes:", sizes);

    if (sizes.length === 0) {
        document.getElementById("selectedTotalSize").innerText =
            "Total size: 0";
        return;
    }

    const selectedUnit = $('#sizeUnit').val();

    let totalBytes = 0;

    // ------------------------------------------------
    // Fixed units
    // ------------------------------------------------

    if (selectedUnit === "bytes") {

        sizes.forEach(function(size) {
            totalBytes += size.bytes;
        });

        const total = totalBytes;

        document.getElementById("selectedTotalSize").innerText =
            "Total size: " + total.toFixed(2) + " B";

        return;
    }

    if (selectedUnit === "kilobytes") {

        sizes.forEach(function(size) {
            totalBytes += size.bytes;
        });

        const total = totalBytes / (1024 ** 1);

        document.getElementById("selectedTotalSize").innerText =
            "Total size: " + total.toFixed(2) + " KB";

        return;
    }

    if (selectedUnit === "megabytes") {

        sizes.forEach(function(size) {
            totalBytes += size.bytes;
        });

        const total = totalBytes / (1024 ** 2);

        document.getElementById("selectedTotalSize").innerText =
            "Total size: " + total.toFixed(2) + " MB";

        return;
    }

    if (selectedUnit === "gigabytes") {

        sizes.forEach(function(size) {
            totalBytes += size.bytes;
        });

        const total = totalBytes / (1024 ** 3);

        document.getElementById("selectedTotalSize").innerText =
            "Total size: " + total.toFixed(2) + " GB";

        return;
    }

    if (selectedUnit === "terabytes") {

        sizes.forEach(function(size) {
            totalBytes += size.bytes;
        });

        const total = totalBytes / (1024 ** 4);

        document.getElementById("selectedTotalSize").innerText =
            "Total size: " + total.toFixed(2) + " TB";

        return;
    }

    // ------------------------------------------------
    // Dynamic
    // ------------------------------------------------

    if (selectedUnit === "dynamic") {

        /*
         * Find the largest unit among selected archives.
         *
         * B < KB < MB < GB < TB < PB
         */

        const unitOrder = {
            "B": 0,
            "KB": 1,
            "MB": 2,
            "GB": 3,
            "TB": 4,
            "PB": 5
        };

        let largestUnit = "B";

        sizes.forEach(function(size) {

            if (unitOrder[size.unit] > unitOrder[largestUnit]) {
                largestUnit = size.unit;
            }

        });

        // Conversion multiplier for the largest unit
        const multiplier = {
            "B": 1,
            "KB": 1024,
            "MB": 1024 ** 2,
            "GB": 1024 ** 3,
            "TB": 1024 ** 4,
            "PB": 1024 ** 5
        };

        // Convert everything to the largest unit
        let total = 0;

        sizes.forEach(function(size) {

            total += size.bytes / multiplier[largestUnit];

        });

        document.getElementById("selectedTotalSize").innerText =
            `Total size: ${total.toFixed(2)} ${largestUnit}`;

        return;
    }

    // ------------------------------------------------
    // Original
    // ------------------------------------------------

    if (selectedUnit === "original") {

        /*
         * For Original, find the largest unit and
         * use it as the common unit for the total.
         */

        const unitOrder = {
            "B": 0,
            "KB": 1,
            "MB": 2,
            "GB": 3,
            "TB": 4,
            "PB": 5
        };

        let largestUnit = "B";

        sizes.forEach(function(size) {

            if (unitOrder[size.unit] > unitOrder[largestUnit]) {
                largestUnit = size.unit;
            }

        });

        const multiplier = {
            "B": 1,
            "KB": 1024,
            "MB": 1024 ** 2,
            "GB": 1024 ** 3,
            "TB": 1024 ** 4,
            "PB": 1024 ** 5
        };

        let total = 0;

        sizes.forEach(function(size) {

            total += size.bytes / multiplier[largestUnit];

        });

        document.getElementById("selectedTotalSize").innerText =
            `Total size: ${total.toFixed(2)} ${largestUnit}`;
    }
}

$(document).ready(function () {

    let selectedArchives = new Set();
    let selectedArchiveRows = new Set();

    var table = $('#archiveTable').DataTable();

    // Save selections
    $('#archiveTable tbody').on('change', '.archive-checkbox', function () {

        const archiveName = $(this).val();
        const row = $(this).closest('tr');
        const rowData = table.row($(this).closest('tr')).data();
        const sizeString = row.find('.size-cell').attr('data-original');

        if ($(this).is(':checked')) {

            selectedArchives.add(archiveName);
            const archivePath = row.find("td").eq(3).text().trim();
            selectedRows.push({
                archiveName: archiveName,
                archivePath: archivePath,
                size: sizeString
            });
        } else {

            selectedRows = selectedRows.filter(function(row) {
                return row.archiveName !== archiveName;
            });
            selectedArchives.delete(archiveName);
        }

        console.log(Array.from(selectedArchives));
        updateSelectedCount(selectedArchives); 
        updateSelectedTotalSize();
		// Redraw table in case filter mode is active
        if (showOnlySelected) {
            table.draw();
        }
   });

    // Restore selections after pagination/search/sort
    table.on('draw', function () {

        $('.archive-checkbox').each(function () {

            const archiveName = $(this).val();
            if (selectedArchives.has(archiveName)) {
                $(this).prop('checked', true);
            }
        });
        updateSelectedCount(selectedArchives);
        updateSelectedTotalSize();
    });

let showOnlySelected = false;

// Register custom filter once
$.fn.dataTable.ext.search.push(function (settings, data, dataIndex) {

    if (settings.nTable.id !== "archiveTable" || !showOnlySelected) {
        return true;
    }

    const rowNode = table.row(dataIndex).node();

    if (!rowNode) return false;

    const archiveName = String(
        $(rowNode).find(".archive-checkbox").val() || ""
    );

    const archivePath = $(rowNode).find("td").eq(3).text().trim();

    return selectedRows.some(function (selected) {
        return selected.archiveName === archiveName &&
               selected.archivePath === archivePath;
    });
});

// Toggle button
$('#toggle-filter').click(function(e){

    e.preventDefault();

    showOnlySelected = !showOnlySelected;

    $(this).text(
        showOnlySelected ? "All Archives" : "Filter"
    );

    table.draw();
});

function formatDynamicSize(sizeString) {

    if (!sizeString) {
        return "";
    }

    sizeString = sizeString.toString().trim();

    const parts = sizeString.split(/\s+/);

    // Remove thousands separators
    const value = parseFloat(parts[0].replace(/,/g, ""));

    const unit = parts[1].toUpperCase();

    if (isNaN(value)) {
        return sizeString;
    }

    const multipliers = {
        "B": 1,
        "KB": 1024,
        "MB": 1024 ** 2,
        "GB": 1024 ** 3,
        "TB": 1024 ** 4
    };

    if (!(unit in multipliers)) {
        return sizeString;
    }

    // Convert everything to bytes
    const bytes = value * multipliers[unit];

    // Dynamically choose the appropriate unit
    if (bytes < 1024) {
        return bytes.toFixed(2) + " B";
    }
    else if (bytes < 1024 ** 2) {
        return (bytes / 1024).toFixed(2) + " KB";
    }
    else if (bytes < 1024 ** 3) {
        return (bytes / (1024 ** 2)).toFixed(2) + " MB";
    }
    else if (bytes < 1024 ** 4) {
        return (bytes / (1024 ** 3)).toFixed(2) + " GB";
    }
    else if (bytes < 1024 ** 5) {
        return (bytes / (1024 ** 4)).toFixed(2) + " TB";
    }
    else {
        return (bytes / (1024 ** 5)).toFixed(2) + " PB";
    }
}

function updateSizeDisplay() {

    var selectedUnit = $('#sizeUnit').val();

    $('.size-cell').each(function () {

        if (selectedUnit === 'dynamic') {

            // Get original size, e.g. "41 KB"
            var originalSize = $(this).data('original');

            var dynamicSize = formatDynamicSize(originalSize);

            $(this).text(dynamicSize);

        } 
        else {

            var size = $(this).data(selectedUnit);

            if (size !== undefined) {

                $(this).text(
                    size +
                    (
                        selectedUnit === 'gigabytes' ? ' GB' :
                        selectedUnit === 'kilobytes' ? ' KB' :
                        selectedUnit === 'megabytes' ? ' MB' :
                        ''
                    )
                );
            }
        }
    });
}

    $('#sizeUnit').change(updateSizeDisplay);
    table.on('draw', updateSizeDisplay);
    updateSizeDisplay();
    updateSelectedTotalSize();

    $('#retrieve-selected').click(function () {

        if (Array.from(selectedArchives).length === 0) {
            showPopup("Error: No Archives Selected! Please select the archives to proceed", "error");
            return;
        }
        showRetrieveModal(Array.from(selectedArchives));
    });



$(document).ready(function () {
            // Open modal when the button is clicked
            $("#remove-selected").click(function () {
                var selectedArchives = $('.archive-checkbox:checked').map(function () { return this.value; }).get();
//
               if (Array.from(selectedArchives).length === 0) {
                  showPopup("Error: No archives selected!", "error");
                  return;
                }
                $("#confirmationModal").modal("show");
            });
});


// Handle OK button click
            $("#okButton1").click(function () {
//               var selectedArchives = $('.archive-checkbox:checked').map(function () { return this.value; }).get();
               
                var inputValue = $("#modalInput").val();
               
                if (inputValue.trim() === "") {
                    alert("Please enter a value.");
                } else if (inputValue.trim() === "Yes" || inputValue.trim() === "yes"){
                    $("#confirmationModal").modal("hide");
                    $("#confirmationModal2").modal("show");
                    let message = "";
                    message += "<b>Archives:</b><br>" +
                        Array.from(selectedArchives).join("<br>") +
                        "<br><br>";
                    $("#confirmationModalBody").html(message);
                } else if (inputValue.trim() === "No"){
                    $("#confirmationModal").modal("hide");
                } else if (inputValue.trim() !== "Yes" || inputValue.trim !== "No"){
                    alert("Please enter exact value Yes or No");
                }


            });

// Handle OK button click
            $("#okButton2").click(function () {
//               var selectedArchives.map(function () { return this.value; }).get();

$("#confirmationModal2").modal("hide");               
$("#loader").show();

startProgressPolling();
console.log("AJAX Request Started ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â°ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬");

    $.ajax({
    url: '/flask/remove-archives',
    type: 'POST',
    contentType: 'application/json',
    data: JSON.stringify({
        archives: Array.from(selectedArchives)
    }),
    success: function (response) {
        stopProgressPolling();
        $("#loader").hide();
        console.log("DEBUG: Raw Response from Backend:", response);

        if (!response || typeof response !== "object") {
            showPopup("Error: Invalid server response!", "error");
            return;
        }
let message = "";

        console.log("DEBUG: Removed Data Type:", typeof response.Removed);
if (response.Removed.length > 0 && response.failedRemovals.length === 0) {
                    message += "<b>The following archives have been removed:</b><br>" + response.Removed.join("<br>") + "<br><br>";
                    showPopup(message, "success");
                }
else if(response.failedRemovals.length > 0 && response.Removed.length === 0) {
                    message += "<b> Error in removing following archives: </b><br>" + response.failedRemovals.join("<br>") + "<br><br>";
                    showPopup(message, "success");
                }
else if(response.failedRemovals.length > 0 &&  response.Removed.length > 0) {
                   message += "<b>The following archives have been removed:</b><br>" + response.Removed.join("<br>") + "<br><br>" + "<b> Error in removing following archives: </b><br>" + response.failedRemovals.join("<br>") + "<br><br>";
                   console.log("Message:", message);
                    showPopup(message, "success");
                }
    },
    error: function (xhr, status, error) {
        stopProgressPolling();
        $("#loader").hide();
        console.log("AJAX Error: ", error);
        showPopup("AJAX Request Failed: " + error, "error");
    }
});
location.reload();
        });



// ÃƒÂ¢Ã…â€œÃ¢â‚¬Â¦ Function to Show Bootstrap Modal with Dynamic Buttons
function showPopup(message, type = "success", selectedArchives = []) {
    $("#messageModalBody").html(message);

    let footerHtml = '<button type="button" id="closeBtn1" class="btn btn-secondary" data-bs-dismiss="modal">Close</button>';

    if (type === "retrieval" && selectedArchives.length > 0) {
        footerHtml = `
            <button type="button" class="btn btn-danger" id="force-retrieve">Force Retrieve</button>
            <button type="button" id="closeBtn2"  class="btn btn-secondary" data-bs-dismiss="modal">Close</button>
        `;
    }

    $("#messageModalFooter").html(footerHtml);
    $("#messageModal").modal("show");

// ÃƒÆ’Ã‚Â¢Ãƒâ€¦Ã¢â‚¬Å“ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ Handle close Click
    $("#closeBtn1").off("click").on("click", function () {
        location.reload();
        console.log("Inside close button");
    });

// ÃƒÂ¢Ã…â€œÃ¢â‚¬Â¦ Handle Force Retrieve Click
    $("#force-retrieve").off("click").on("click", function () {
        forceRetrieve(selectedArchives);
    });
}

// ÃƒÂ¢Ã…â€œÃ¢â‚¬Â¦ Function to Handle Force Retrieval
function forceRetrieve(selectedArchives) {
    $("#messageModal").modal("hide");
    $("#loader").show();
    $.ajax({
        url: "/flask/force-retrieve",
        type: "POST",
        contentType: "application/json",
        data: JSON.stringify({ archives: selectedArchives, extract: extractChecked }),
        success: function (response) {
            console.log("Force Retrieval response: ", response);
            showPopup("Force retrieval completed successfully.", "success");
        },
        error: function (xhr, status, error) {
            showPopup("Force retrieval failed: " + error, "error");
        },
        complete: function () {
            // Hide the loader when request completes (either success or failure)
            $("#loader").hide();
        }
    });
}

function showRetrieveModal(selectedArchives) {
  fetch("/flask/check-retrieved", {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({ archives: selectedArchives })
  })
  .then(response => response.json())
  .then(data => {
   alreadyRetrieved = data.alreadyRetrieved || [];
  const listContainer = document.getElementById('selectedArchivesList');
  listContainer.innerHTML = "";

  selectedArchives.forEach((name) => {
      const isRetrieved = alreadyRetrieved.includes(name);

      const row = document.createElement("div");
      row.className = "archive-row d-flex justify-content-between align-items-center mb-2";

      row.innerHTML = `
          <div class="d-flex align-items-center">

              <!-- Main selection -->
              <div class="form-check me-3">
                  <input class="form-check-input modal-select-checkbox"
                         type="checkbox"
                         value="${name}"
                         id="select-${name}"
                         checked>

                  <label class="form-check-label" for="select-${name}">
                      ${name}
                  </label>
              </div>

          </div>

          ${
              isRetrieved
              ? `
              <div class="form-check">
                  <input class="form-check-input force-retrieve-checkbox"
                         type="checkbox"
                         value="${name}"
                         id="force-${name}">
                  <label class="form-check-label" for="force-${name}">
                      Force Retrieve
                  </label>
              </div>
              `
              : ""
          }
      `;

      listContainer.appendChild(row);
  });
  const modalElement = document.getElementById('archiveModal');
  const modal = new bootstrap.Modal(modalElement);
  modal.show();
  })
  .catch(error => {
    console.error("Error checking retrieved archives:", error);
  });
}

const extractInput = document.getElementById("extractPath");

extractInput.addEventListener("blur", function () {
    let extractPath = extractInput.value.trim();

    if (extractPath) {
        if (!extractPath.startsWith("/")) {
            extractPath = "/" + extractPath;
        }
        if (!extractPath.endsWith("/")) {
            extractPath += "/";
        }
    }

    // Update the input field with the corrected path
    extractInput.value = extractPath;
});


const retrieveInput = document.getElementById("customRetrievePath");

retrieveInput.addEventListener("blur", function () {
    let customRetrievePath = retrieveInput.value.trim();

    if (customRetrievePath) {
        if (!customRetrievePath.startsWith("/")) {
            customRetrievePath = "/" + customRetrievePath;
        }
        if (!customRetrievePath.endsWith("/")) {
            customRetrievePath += "/";
        }
    }

    // Update the input field with the corrected path
    retrieveInput.value = customRetrievePath;
});

async function createPath(path) {
    const response = await fetch("/flask/create-path", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: path })
    });

    const data = await response.json();
    return data.success;
}

async function checkPath(inputElement, errorElementId) {
    if (!inputElement) return true;

    let path = inputElement.value;
    const errorElement = document.getElementById(errorElementId);

    if (errorElement) {
        errorElement.style.display = "none";
        errorElement.innerText = "";
    }

    if (!path) return true;

    // Normalize path
    if (!path.startsWith("/")) path = "/" + path;
    if (!path.endsWith("/")) path += "/";
    inputElement.value = path;

    try {
        const response = await fetch("/flask/check-path", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path }),
        });

        if (!response.ok) throw new Error("Server returned " + response.status);

        const data = await response.json();
        console.log("Ã¢Å“â€¦ Received from backend:", data);
        console.log("Ã°Å¸Â§Âª data.exists type:", typeof data.exists);

        // Use strict comparison and handle possible string 'false'
        if (data.exists === false || data.exists === "false") {
            if (errorElement) {
                errorElement.innerText =
                    `This path does not exist. Press "Confirm" to create new directory or enter a valid path.`;
                errorElement.style.display = "block";
            }
            return false;
        }

    } catch (err) {
        console.error("Ã¢Å’ Error checking path:", err);
        if (errorElement) {
            errorElement.innerText = "Failed to check path. Try again.";
            errorElement.style.display = "block";
        }
        return false;
    }
}


// Attach event to both inputs
document.getElementById("extractPath").addEventListener("blur", function() {
    checkPath(this, "extractPathError");
});

document.getElementById("customRetrievePath").addEventListener("blur", function() {
    checkPath(this, "retrievePathError");
});


let progressInterval;

function startProgressPolling() {
  // show progress bar UI
  $("#progressBarContainer").show();
  $("#progressText").text("Starting...");

  // start polling the backend
  pollInterval = setInterval(() => {
    $.get("/flask/progress", function (data) {
      if (data && data.length > 0) {
        const latest = data[data.length - 1];
        updateProgressBar(latest);
      }
    });
  }, 1000);
}

function stopProgressPolling() {
  // stop polling
  clearInterval(pollInterval);
  pollInterval = null;

  // hide the bar or mark as complete
  $("#progressText").text("Ã¢Å“â€¦ Done!");
  setTimeout(() => {
    $("#progressBarContainer").fadeOut();
  }, 2000);
}

function updateProgressBar(message) {
  // 1. update UI text
  $("#progressText").text(message);
// 2. Visuals for the Green Bar
  // Since we don't have a %, we just make the bar look "active"
  if (message.includes("Retrieved...") && !message.includes("Ã¢Å“â€¦")) {
      // Make the bar 100% width but maybe a different color or striped to show activity
      $("#progressBar").css("width", "100%"); 
      $("#progressBar").css("background-color", "#17a2b8"); // Blue for active downloading
  } 
  else if (message.includes("Ã¢Å“â€¦") || message.includes("Done")) {
      // Success state
      $("#progressBar").css("width", "100%");
      $("#progressBar").css("background-color", "#00ff66"); // Green for success
  }
}


//  Handle Force Retrieve Click
    $("#confirm-retrieve").off("click").on("click", async function () {

//        var selectedArchives = $('.archive-checkbox:checked').map(function () { return this.value; }).get();
          const customRetrievePath = document.getElementById("customRetrievePath").value;
  const extractChecked = document.getElementById("extractCheckbox").checked;
  const extractPath = document.getElementById("extractPath").value;
  const removeChecked = document.getElementById("removeCheckbox").checked;

  const archiveNames = Array.from(
      document.querySelectorAll(".modal-select-checkbox:checked")
  ).map(cb => cb.value);

  const selectedForceArchives = Array.from(
      document.querySelectorAll(".force-retrieve-checkbox:checked")
  )
  .map(cb => cb.value)
  .filter(name => archiveNames.includes(name));

  // Filter only archives that are NOT already retrieved
  const notRetrievedArchives = archiveNames.filter(
      name => !alreadyRetrieved.includes(name)
  );

const customRetrievePathInput = document.getElementById("customRetrievePath");

  if (customRetrievePathInput && customRetrievePathInput.value) {
      console.log("Checking if the custom retrieve path exists or not");
      const exists = await checkPath(customRetrievePathInput, "retrievePathError");
      console.log("Following is returned by checkpath function"+exists);
      if (!exists) {
          const created = await createPath(customRetrievePathInput.value);
          if (!created) {
              showPopup("Failed to create retrieve path!", "error");
              return; // Stop further execution
          }
      }
  }

const extractInput = document.getElementById("extractPath");

if (extractInput && extractInput.value) {
    console.log("Checking if extract path exists or not");
    const exists = await checkPath(extractInput, "extractPathError");
    console.log("following is returned by checkPath function:", exists);

    if (exists == false) {
        const created = await createPath(extractInput.value);
        if (!created) {
            showPopup("Failed to create extract path!", "error");
            return;
        }
    }
}


 const payload = {
    retrievePath: customRetrievePath,
    extract: extractChecked,
    extractPath: extractPath,
    remove: removeChecked,
    archives: archiveNames,
    forceArchives: selectedForceArchives,
    UnretrievedArchives: notRetrievedArchives
  };

  console.log("Final Data to send:", payload);

  startProgressPolling();
  if(customRetrievePath==''  && (notRetrievedArchives!=null || selectedForceArchives!=null)) {
    $("#loader").show();
    console.log("AJAX Request Started ÃƒÆ’Ã‚Â°Ãƒâ€¦Ã‚Â¸Ãƒâ€¦Ã‚Â¡ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬");

    $.ajax({
    url: '/flask/retrieve',
    type: 'POST',
    contentType: 'application/json',
    data: JSON.stringify({ archives: notRetrievedArchives, extract: extractChecked, extract_Path: extractPath || null, remove: removeChecked,  forceArchives: selectedForceArchives }),
    success: function (response) {
        stopProgressPolling();
        $("#loader").hide();
        console.log("DEBUG: Raw Response from Backend:", response);

        if (!response || typeof response !== "object") {
            showPopup("Error: Invalid server response!", "error");
            return;
        }
    let message = "";

        console.log("DEBUG: alreadyRetrieved Data Type:", typeof response.alreadyRetrieved);
        console.log("DEBUG: alreadyRetrieved Value:", response.alreadyRetrieved);
        console.log("DEBUG: extractionSuccess value:", response.extractionSuccess);
    if (response.SuccessMessage.length > 0) {
                    showPopup(response.SuccessMessage, "success");
                }

    else {
                message += "<b>Error in retrieving archives:</b><br>";
                    showPopup(message, "success");
            }

    },
    error: function (xhr, status, error) {
        stopProgressPolling();
        $("#loader").hide();
        console.log("AJAX Error: ", error);
        showPopup("AJAX Request Failed: " + error, "error");
    }
});
   }


   else if(customRetrievePath!='' && (notRetrievedArchives!=null || selectedForceArchives!=null)){
    $("#loader").show();
    startProgressPolling();
    console.log("AJAX Request Started");

    $.ajax({
    url: '/flask/retrieve-change-path',
    type: 'POST',
    contentType: 'application/json',
    data: JSON.stringify({ archives: notRetrievedArchives, retrieve_path: customRetrievePath, extract: extractChecked, extract_Path: extractPath || null, remove: removeChecked,  forceArchives: selectedForceArchives  }),
    success: function (response) {
        stopProgressPolling();
        $("#loader").hide();
        console.log("DEBUG: Raw Response from Backend:", response);

        if (!response || typeof response !== "object") {
            showPopup("Error: Invalid server response!", "error");
            return;
        }
    if (response.SuccessMessage.length > 0) {
                    showPopup(response.SuccessMessage, "success");
                }

    else {
                message += "<b>Error in retrieving archives:</b><br>";
                    showPopup(message, "success");
            }


    },
    error: function (xhr, status, error) {
        stopProgressPolling();
        $("#loader").hide();
        console.log("AJAX Error: ", error);
        showPopup("AJAX R12:00:00 equest Failed: " + error, "error");
    }

});
   }


   else if(selectedForceArchives!= null && notRetrievedArchives== null ) {
    $("#loader").show();
    startProgressPolling();
    console.log("AJAX Request Started ");

    $.ajax({
    url: '/flask/force-retrieve',
    type: 'POST',
    contentType: 'application/json',
    data: JSON.stringify({ archives: selectedForceArchives, extract: extractChecked, customExtractPath: extractPath  }),
        success: function (response) {
            stopProgressPolling();
            console.log("Force Retrieval response: ", response);
            showPopup("Force retrieval completed successfully.", "success");
        },
        error: function (xhr, status, error) {
            showPopup("Force retrieval failed: " + error, "error");
        },
        complete: function () {
            // Hide the loader when request completes (either success or failure)
            stopProgressPolling();
            $("#loader").hide();
        }
});
   }


  document.getElementById('archiveModal').style.display = 'none';
    });


document.getElementById("extractCheckbox").addEventListener("change", function () {
  const removeCheckbox = document.getElementById("removeCheckbox");
  const extractPath = document.getElementById("extractPath");

  if (this.checked) {
    removeCheckbox.disabled = false;
    extractPath.disabled = false;
  } else {
    removeCheckbox.disabled = true;
    extractPath.disabled = true;
    removeCheckbox.checked = false;
    extractPath.value = "";
  }
});
});

</script>
        </body>
        </html>
        """,
        archives=archives
    )

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=8081,
        debug=True,
        use_reloader=False
    )

