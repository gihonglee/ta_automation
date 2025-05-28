from openai import OpenAI
import fitz 
import json
import io
import re
import os
from PIL import Image

from google.auth import default
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# Try to import config.py for local dev
try:
    from config import FOLDER_ID, SPREADSHEET_ID, OPENAI_API_KEY, SERVICE_ACCOUNT_FILE
    LOCAL_DEV = True
except ImportError:
    # Fallback for deployed Cloud Function
    LOCAL_DEV = False
    FOLDER_ID = os.getenv("FOLDER_ID")
    SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    SERVICE_ACCOUNT_FILE = None  # not used in deployment

SCOPES = ['https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/spreadsheets']

ORDERED_FIELDS = [
    "index",
    "name",
    "resume_link",
    "industry",
    "experience",
    "current_location",
    "email",
    "phone",
    "linkedin",
    "current_job_title",
    "current_company",
    "education",
    "major",
    "university",
    "location_preference"
]

def get_credentials():
    if LOCAL_DEV:
        return service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    else:
        # Use default service account in Cloud Functions
        creds, _ = default(scopes=SCOPES)
        return creds

creds = get_credentials()
client = OpenAI(api_key = OPENAI_API_KEY)
drive_service = build('drive', 'v3', credentials=creds)
sheets_service = build('sheets', 'v4', credentials=creds)

def list_files_in_folder(drive_service):
    query = f"'{FOLDER_ID}' in parents and mimeType='application/pdf'"
    files = []
    page_token = None

    while True:
        response = drive_service.files().list(
            q=query,
            fields="nextPageToken, files(id, name)",
            pageSize=1000,
            pageToken=page_token
        ).execute()

        files.extend(response.get('files', []))
        page_token = response.get('nextPageToken', None)
        if page_token is None:
            break

    return files


def download_resume_file(drive_service, file_id):
    request = drive_service.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buffer.seek(0)
    return buffer.read()

def extract_text_from_pdf(pdf_bytes, ocr_threshold=50):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    text = ""

    for page in doc:
        page_text = page.get_text()
        text += page_text
    return text

import json

def parse_resume_text(resume_text, file_name):
    prompt = f"""
You are a recruiter assistant. Extract the following fields from the resume text and return the result as a **flat valid JSON object only** (no explanation, no markdown, no comments, no formatting). If any field is missing, return an empty string. Use only the following lowercase keys in the order shown:

[
  "industry",                // example: IT, construction, finance
  "experience",              // total years of work experience as a number
  "current_location",        // only return city and state (e.g., "Austin, TX")
  "email",                   // primary email address
  "phone",                   // phone number
  "linkedin",                // linkedin profile URL
  "current_job_title",       // current or most recent job title
  "current_company",         // current or most recent company
  "education",               // highest level of education: "AA", "Bachelor", "Master", or "PhD"
  "major",                   // the college major or field of study
  "university",              // name of the college or university
  "location_preference"      // preferred location to work (if stated)
]

⚠️ Return only a valid JSON object in this exact order of keys. No explanation. No markdown. No text before or after.

Resume file: {file_name}

Resume text:
{resume_text[:10000]}
    """

    response = client.responses.create(model="gpt-4.1", input=prompt)
    output = response.output_text.strip()

    try:
        return json.loads(output)
    except json.JSONDecodeError:
        print("❌ First attempt failed to parse JSON. Retrying...")

        retry_prompt = prompt + "\n\n⚠️ One more time, only return a valid flat JSON object with the keys in exact order and lowercase. Do not include any extra text."
        retry_response = client.responses.create(model="gpt-4.1", input=retry_prompt)
        retry_output = retry_response.output_text.strip()

        try:
            return json.loads(retry_output)
        except json.JSONDecodeError:
            print("❌ Second attempt also failed. Output was:")
            print(retry_output)
            raise 

def append_to_sheet(sheets_service, file_id, parsed_json, file_name):
    import re
    import json

    # Remove extension first, then extract index and name
    file_name_no_ext = os.path.splitext(file_name.strip())[0]

    # Extract index and name from cleaned file name
    match = re.match(r"(\d+)\.\s*(.+)", file_name_no_ext)
    if match:
        index_number = match.group(1)  # e.g., "167"
        name = match.group(2)          # e.g., "David Kim"
    else:
        index_number = ""
        name = ""

    resume_link = f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"
    data = parsed_json

    def sanitize_field(value):
        if isinstance(value, (list, dict)):
            return json.dumps(value)
        return value

    ordered_row = [index_number,name, resume_link] + [
        sanitize_field(data.get(field, "")) for field in ORDERED_FIELDS[3:]
    ] 
    
    sheets_service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range='raw_output!A2',
        valueInputOption='RAW',
        body={'values': [ordered_row]}
    ).execute()


def write_sheet_header(sheets_service):
    header_row =  ORDERED_FIELDS
    sheets_service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range='raw_output!A1',
        valueInputOption='RAW',
        body={'values': [header_row]}
    ).execute()


def run_resume_pipeline_batch(drive_service, sheets_service):
    write_sheet_header(sheets_service)
    files = list_files_in_folder(drive_service)
    print(len(files))
    # Sort files by the numeric prefix (e.g., "1. Name" -> 1)
    def extract_index(file):
        match = re.match(r"(\d+)", file["name"].strip())
        return int(match.group(1)) if match else float("inf")  # Put unsortable names at the end

    files.sort(key=extract_index)
    for file in files:
        file_id = file['id']
        file_name = file['name']
        print(file_name)
        file_bytes = download_resume_file(drive_service, file_id)
        resume_text = extract_text_from_pdf(file_bytes)
        gpt_output = parse_resume_text(resume_text, file_name)
        append_to_sheet(sheets_service, file_id, gpt_output, file_name)

# run_resume_pipeline_batch(drive_service, sheets_service)

def run_resume_pipeline_single(file_id):
    # Get file metadata
    file = drive_service.files().get(fileId=file_id, fields="id, name").execute()
    file_name = file["name"]

    print(f"📄 Running on single file: {file_name}")
    file_bytes = download_resume_file(drive_service, file_id)
    resume_text = extract_text_from_pdf(file_bytes)
    gpt_output = parse_resume_text(resume_text, file_name)
    append_to_sheet(sheets_service, file_id, gpt_output, file_name)

# if __name__ == "__main__":
#     run_resume_pipeline_single(file_id="")

# Entry point for Google Cloud Function
def main(request):
    try:
        request_json = request.get_json(silent=True)
        file_id = request_json.get("file_id")
        if not file_id:
            return ("Missing file_id", 400)

        run_resume_pipeline_single(file_id)
        return ("✅ Resume processed successfully", 200)
    except Exception as e:
        return (f"❌ Error: {str(e)}", 500)
    