import os
from PyPDF2 import PdfReader, PdfWriter
import time
from dotenv import load_dotenv
import google.generativeai as genai
from md2pdf.core import md2pdf
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
import io
import re

# Load environment variables from .env file
load_dotenv()

# Get the API key and directory path from the environment variables
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
DIRECTORY_PATH = os.getenv('DIRECTORY_PATH', '')

# Directory containing the temporary files
STAGE_DIRECTORY = "stage"

genai.configure(api_key=GEMINI_API_KEY)

def get_page_number(file_path):
    """
    Extracts the page number from the filename.
    Args:
        filename (str): The name of the file.
    Returns:
        int: The extracted page number. Returns a large number if no match is found to push such files to the end.
    """
    match = re.search(r'pages-(\d+)', file_path)
    if match:
        return int(match.group(1))
    return float('inf')  # Files without a page number are placed at the end

def upload_to_gemini(path, mime_type=None):
    """Uploads the given file to Gemini.

    See https://ai.google.dev/gemini-api/docs/prompting_with_media
    """
    file = genai.upload_file(path, mime_type=mime_type)
    print(f"Uploaded file '{file.display_name}' as: {file.uri}")
    return file

def wait_for_files_active(files):
    """Waits for the given files to be active.

    Some files uploaded to the Gemini API need to be processed before they can be
    used as prompt inputs. The status can be seen by querying the file's "state"
    field.

    This implementation uses a simple blocking polling loop. Production code
    should probably employ a more sophisticated approach.
    """
    print("Waiting for file processing...")
    for name in (file.name for file in files):
        file = genai.get_file(name)
        while file.state.name == "PROCESSING":
            print(".", end="", flush=True)
            time.sleep(5)
            file = genai.get_file(name)
        if file.state.name != "ACTIVE":
            raise Exception(f"File {file.name} failed to process")

def create_final_page(text, output_path):
    packet = io.BytesIO()
    can = canvas.Canvas(packet, pagesize=letter)
    width, height = letter
    can.drawCentredString(width / 2.0, height / 2.0, text)
    can.save()

    packet.seek(0)
    new_pdf = PdfReader(packet)
    output = PdfWriter()

    for page in new_pdf.pages:
        output.add_page(page)

    with open(output_path, "wb") as outputStream:
        output.write(outputStream)

from google.generativeai import GenerationConfig

generation_config = GenerationConfig(
    temperature=1,
    top_p=0.95,
    top_k=64,
    max_output_tokens=8192,
    response_mime_type="text/plain",
)

model = genai.GenerativeModel(
    model_name="gemini-2.0-flash",
    generation_config=generation_config,
    # safety_settings = Adjust safety settings
    # See https://ai.google.dev/gemini-api/docs/safety-settings
)

# List all files in the directory
files_in_directory = os.listdir(DIRECTORY_PATH)

# Filter files if necessary (e.g., only PDF files)
pdf_files = [f for f in files_in_directory if f.endswith('.pdf')]

# Implement numerical sorting based on the extracted page numbers
pdf_files.sort(key=get_page_number)

# Define the prompt template
prompt_template = """
I am uploading a PDF with single page of a Kannada Text Book. You have to read this text paragraph by paragraph
For each paragraph we will do 3 steps
Step 1: Print the Kannada text in Kannada Script.
Step 2: Print the Kannada text in English Script.
Step 3: Print the translation of Kannada text in English Script.
Combine all the paragraph outputs in a single markdown file.

Updated instructions for future files.
"""

chat_session = model.start_chat()
response = chat_session.send_message(prompt_template)
print(response.text)

# Uploading and Processing PDFs

for file in pdf_files:
    print(f"Processing file: {file}")

    # Define the expected appended PDF file name
    base_name = os.path.splitext(file)[0]
    appended_pdf_file_name = os.path.join(STAGE_DIRECTORY, f"{base_name}_appended.pdf")

    # Check if the appended PDF already exists
    if os.path.exists(appended_pdf_file_name):
        print(f"Output file '{appended_pdf_file_name}' already exists. Skipping translation for '{file}'.")
        continue  # Skip to the next file

    print(f"Uploading file: {file}")
    uploaded_file = upload_to_gemini(os.path.join(DIRECTORY_PATH, file), mime_type="application/pdf")
    print(f"Uploaded file: {uploaded_file}")

    # Wait for the file to be processed
    wait_for_files_active([uploaded_file])

    response = chat_session.send_message(uploaded_file)
    print(response.text)

    # Save the response as a .md file with name same as the uploaded file
    response_file_name = os.path.join(STAGE_DIRECTORY, f"{base_name}.md")
    
    # Check if the Markdown file already exists
    if os.path.exists(response_file_name):
        print(f"Markdown file '{response_file_name}' already exists. Skipping Markdown creation for '{file}'.")
    else:
        with open(response_file_name, "w") as f:
            f.write(response.text)
        print(f"Saved response as: {response_file_name}")

    # Convert markdown to PDF using md2pdf
    pdf_file_name = os.path.join(STAGE_DIRECTORY, f"{base_name}_response.pdf")
    
    # Check if the response PDF already exists
    if os.path.exists(pdf_file_name):
        print(f"Response PDF '{pdf_file_name}' already exists. Skipping PDF conversion for '{file}'.")
    else:
        md2pdf(pdf_file_name, md_content=response.text)
        print(f"Saved PDF as: {pdf_file_name}")

    # Append the generated PDF to the uploaded PDF
    if os.path.exists(appended_pdf_file_name):
        print(f"Appended PDF '{appended_pdf_file_name}' already exists. Skipping PDF appending for '{file}'.")
    else:
        with open(os.path.join(DIRECTORY_PATH, file), "rb") as original, open(pdf_file_name, "rb") as response_pdf:
            original_pdf_reader = PdfReader(original)
            response_pdf_reader = PdfReader(response_pdf)
            pdf_writer = PdfWriter()

            for page_num in range(len(original_pdf_reader.pages)):
                pdf_writer.add_page(original_pdf_reader.pages[page_num])

            for page_num in range(len(response_pdf_reader.pages)):
                pdf_writer.add_page(response_pdf_reader.pages[page_num])

            with open(appended_pdf_file_name, "wb") as output_pdf:
                pdf_writer.write(output_pdf)
        print(f"Appended PDF saved as: {appended_pdf_file_name}")

# Output file name
output_file_name = "Final.pdf"

# List all files in the stage directory
files_in_stage = os.listdir(STAGE_DIRECTORY)

# Filter files to include only *_appended.pdf files
appended_pdf_files = [f for f in files_in_stage if f.endswith('_appended.pdf')]

# Sort the appended PDFs using the same sort key as input files
appended_pdf_files.sort(key=get_page_number)

# Create a PdfWriter object
pdf_writer = PdfWriter()

# Iterate over each appended PDF file and add its pages to the PdfWriter
for file in appended_pdf_files:
    file_path = os.path.join(STAGE_DIRECTORY, file)
    pdf_reader = PdfReader(file_path)
    for page_num in range(len(pdf_reader.pages)):
        pdf_writer.add_page(pdf_reader.pages[page_num])

final_page_pdf_path = os.path.join(STAGE_DIRECTORY, "finalPage.pdf")
final_page_text = "Generated Using code from https://github.com/wnds/transliteration-kannanda-school-book. Thank You"
create_final_page(final_page_text, final_page_pdf_path)

pdf_reader = PdfReader(final_page_pdf_path)
pdf_writer.add_page(pdf_reader.pages[0])

# Write the combined PDF to the output file
with open(output_file_name, "wb") as output_pdf:
    pdf_writer.write(output_pdf)

print(f"Final merged PDF saved as: {output_file_name}")