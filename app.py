from flask import Flask, render_template, request, redirect, url_for, session, send_from_directory, Markup, flash, send_file
from flask_session import Session 
from azure.ai.formrecognizer import DocumentAnalysisClient
import openai
import os
import requests
import json
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge
import docx2txt
import markdown
import bleach
from docx import Document
from azure.cosmos import CosmosClient, PartitionKey, exceptions
import uuid
import datetime
import msal
from msal import ConfidentialClientApplication
from azure.core.credentials import AzureKeyCredential
from io import BytesIO

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_KEY")
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB limit
app.config['VERSION'] = '0.18'
app.config['SESSION_TYPE'] = 'filesystem'
Session(app)

# Initialize Cosmos client
cosmos_endpoint = os.getenv("AZURE_COSMOS_ENDPOINT")
cosmos_key = os.getenv("AZURE_COSMOS_KEY")
cosmos_db_name = os.getenv("AZURE_COSMOS_DB_NAME")
cosmos_results_container_name = os.getenv("AZURE_COSMOS_RESULTS_CONTAINER_NAME")
cosmos_files_container_name = os.getenv("AZURE_COSMOS_FILES_CONTAINER_NAME")

cosmos_client = CosmosClient(cosmos_endpoint, cosmos_key)
cosmos_db = cosmos_client.create_database_if_not_exists(id=cosmos_db_name)
cosmos_results_container = cosmos_db.create_container_if_not_exists(
    id=cosmos_results_container_name,
    partition_key=PartitionKey(path="/id"),
    offer_throughput=400
)
cosmos_files_container = cosmos_db.create_container_if_not_exists(
    id=cosmos_files_container_name,
    partition_key=PartitionKey(path="/id"),
    offer_throughput=400
)

document_intelligence_client = DocumentAnalysisClient(
    endpoint=os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"),
    credential=AzureKeyCredential(os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY"))
)

# Configure Azure OpenAI
openai.api_type = os.getenv("AZURE_OPENAI_API_TYPE")
openai.api_base = os.getenv("AZURE_OPENAI_ENDPOINT")
openai.api_version = os.getenv("AZURE_OPENAI_API_VERSION")
openai.api_key = os.getenv("AZURE_OPENAI_KEY")
MODEL = os.getenv("AZURE_OPENAI_MODEL")

CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("MICROSOFT_PROVIDER_AUTHENTICATION_SECRET")
TENANT_ID = os.getenv("TENANT_ID")
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPE = ["User.Read"]  # Adjust scope as needed

ALLOWED_EXTENSIONS = {'txt', 'md', 'docx', 'pdf'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.errorhandler(RequestEntityTooLarge)
def handle_file_too_large(e):
    return render_template('upload_content.html', error="File is too large. Maximum allowed size is 16MB."), 413

def save_results_to_cosmos(results, topic, body):
    user_info = session.get("user", {})
    user_id = user_info.get("oid", "anonymous")
    user_email = user_info.get("email", "no-email")

    result_id = str(uuid.uuid4())
    # Extract hardware_name from results if present
    hardware_name = results.get('hardware_name', 'Unknown Hardware')

    result_data = {
        'id': result_id,
        'user_id': user_id,
        'user_email': user_email,
        'topic': topic,
        'body': body,
        'hardware_name': hardware_name,  # Save at top-level
        'results': results,
        'timestamp': str(datetime.datetime.utcnow())
    }
    
    try:
        cosmos_results_container.create_item(body=result_data)
    except exceptions.CosmosHttpResponseError as e:
        print(f"Error saving to Cosmos DB: {e}")

    return result_id

def extract_content_with_azure_di(file_path):
    try:
        with open(file_path, "rb") as f:
            poller = document_intelligence_client.begin_analyze_document(
                model_id="prebuilt-read",
                document=f
            )
        result = poller.result()
        extracted_content  = ""

        if result.content:
            extracted_content  = result.content
        else:
            for page in result.pages:
                for line in page.lines:
                    extracted_content  += line.content + "\n"
                extracted_content  += "\n"

        print(f"Content extracted successfully from {file_path}.")
        return extracted_content 

    except Exception as e:
        print(f"Error extracting content with Azure DI: {str(e)}")
        raise

@app.context_processor
def inject_previous_results():
    query = "SELECT c.id, c.topic, c.timestamp FROM c"
    previous_results = []
    try:
        previous_results = list(cosmos_results_container.query_items(
            query=query,
            enable_cross_partition_query=True
        ))
    except Exception as e:
        print(f"Error fetching previous results: {e}")
    return {'previous_results': previous_results}

@app.route('/select_file', methods=['GET', 'POST'])
def select_file():
    if request.method == 'POST':
        selected_file_id = request.form.get('selected_file')
        if selected_file_id:
            try:
                file_item = cosmos_files_container.read_item(item=selected_file_id, partition_key=selected_file_id)
                session['body'] = file_item['content']
                session['topic'] = extract_topic_from_content(file_item['content'])
                return redirect(url_for('options'))
            except Exception as e:
                print(f"Error retrieving file: {e}")
                flash("An error occurred while retrieving the file.", "danger")
                return redirect(request.url)
    
    sort_by = request.args.get('sort_by', 'timestamp')
    sort_order = request.args.get('sort_order', 'desc')
    search_query = request.args.get('search', '').lower()
    user_id = session.get("user", {}).get("oid", "anonymous")

    files_query = f"SELECT c.id, c.filename, c.timestamp FROM c WHERE c.user_id = '{user_id}'"
    files = list(cosmos_files_container.query_items(query=files_query, enable_cross_partition_query=True))

    if search_query:
        files = [file for file in files if search_query in file['filename'].lower()]

    if sort_by == 'filename':
        files.sort(key=lambda x: x['filename'].lower(), reverse=(sort_order == 'desc'))
    else:
        files.sort(key=lambda x: x['timestamp'], reverse=(sort_order == 'desc'))

    return render_template('select_file.html', files=files, sort_by=sort_by, sort_order=sort_order, search_query=search_query)

@app.template_filter('markdown')
def markdown_filter(text):
    allowed_tags = ['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'ul', 'li', 'ol', 'strong', 'em', 'blockquote', 'code', 'pre']
    allowed_attrs = {}
    html = markdown.markdown(text)
    clean_html = bleach.clean(html, tags=allowed_tags, attributes=allowed_attrs, strip=True)
    return Markup(clean_html)

@app.route("/login")
def login():
    msal_app = ConfidentialClientApplication(CLIENT_ID, authority=AUTHORITY, client_credential=CLIENT_SECRET)
    result = msal_app.get_authorization_request_url(SCOPE, redirect_uri=url_for("authorized", _external=True, _scheme='https'))
    return redirect(result)

@app.route("/getAToken")
def authorized():
    msal_app = ConfidentialClientApplication(CLIENT_ID, authority=AUTHORITY, client_credential=CLIENT_SECRET)
    code = request.args.get('code', '')
    result = msal_app.acquire_token_by_authorization_code(
        code,
        scopes=SCOPE,
        redirect_uri=url_for("authorized", _external=True, _scheme='https')
    )
    if "error" in result:
        return f"Login failure: {result.get('error_description', result.get('error'))}"
    session["user"] = result.get("id_token_claims")
    return redirect(url_for("index"))

@app.route("/logout")
def logout():
    session.clear()
    flash('You have been successfully logged out.', 'success')
    return redirect(url_for('index'))

@app.route('/', methods=['GET', 'POST'])
def index():
     # Clear specific keys instead of the whole session
    keys_to_clear = ['selected_options', 'form_data', 'topic', 'body']
    for key in keys_to_clear:
        session.pop(key, None)
    if request.method == 'POST':
        selected_options = request.form.getlist('options')
        session['selected_options'] = selected_options
        return redirect(url_for('content_method'))
    return render_template('index.html')


@app.route('/content_method', methods=['GET', 'POST'])
def content_method():
    if request.method == 'POST':
        content_option = request.form.get('content_option')
        if content_option == 'provide_text':
            return redirect(url_for('provide_text'))
        elif content_option == 'upload_content':
            return redirect(url_for('upload_content'))
        elif content_option == 'select_file':
            return redirect(url_for('select_file'))
        else:
            flash("Please select a valid content input method.", "danger")
            return redirect(url_for('content_method'))
    return render_template('content_method.html')


@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'), 'favicon.ico', mimetype='image/vnd.microsoft.icon')

@app.route('/history')
def history():
    sort_by = request.args.get('sort_by', 'timestamp')
    sort_order = request.args.get('sort_order', 'desc')
    search_query = request.args.get('search', '').lower()
    user_id = session.get("user", {}).get("oid", "anonymous")

    query = f"SELECT * FROM c WHERE c.user_id = '{user_id}'"
    results = list(cosmos_results_container.query_items(query=query, enable_cross_partition_query=True))

    if search_query:
        results = [result for result in results if search_query in result['topic'].lower()]

    if sort_by == 'topic':
        results.sort(key=lambda x: x['topic'].lower(), reverse=(sort_order == 'desc'))
    else:
        results.sort(key=lambda x: x['timestamp'], reverse=(sort_order == 'desc'))

    return render_template('history.html', results=results, sort_by=sort_by, sort_order=sort_order, search_query=search_query)

@app.route('/result/<result_id>')
def view_result(result_id):
    try:
        result = cosmos_results_container.read_item(item=result_id, partition_key=result_id)
        return render_template('view_result.html', result=result)
    except Exception as e:
        print(f"Error retrieving result: {e}")
        return "Error retrieving result.", 500

@app.route('/provide_text', methods=['GET', 'POST'])
def provide_text():
    if request.method == 'POST':
        topic = request.form.get('topic')
        body = request.form.get('body', '')
        session['topic'] = topic
        session['body'] = body
        return redirect(url_for('options'))
    return render_template('provide_text.html')

@app.route('/options', methods=['GET', 'POST'])
def options():
    body = session.get('body', '')
    if not body:
        flash("No content available. Please provide or upload content first.", "danger")
        return redirect(url_for('index'))

    if request.method == 'POST':
        # User confirmed they want to proceed with the extraction.
        return redirect(url_for('results'))

    # If GET request, render the options template
    return render_template('options.html', body=body)


@app.route('/upload_content', methods=['GET', 'POST'])
def upload_content():
    if request.method == 'POST':
        file = request.files.get('file')
        if file and allowed_file(file.filename):
            try:
                filename = secure_filename(file.filename)
                file_extension = filename.rsplit('.', 1)[1].lower()
                
                if file_extension == 'docx':
                    content = docx2txt.process(file)
                elif file_extension in ['txt', 'md']:
                    content = file.read().decode('utf-8', errors='ignore')
                elif file_extension == 'pdf':
                    # For PDFs, we need to save it temporarily and pass the path to extract_content_with_azure_di
                    temp_path = "/tmp/" + filename
                    file.seek(0)
                    file.save(temp_path)
                    content = extract_content_with_azure_di(temp_path)
                    os.remove(temp_path)
                else:
                    content = ''
                
                if not content.strip():
                    flash("The uploaded file is empty or could not be read.", "danger")
                    return redirect(request.url)
                
                user_info = session.get("user", {})
                user_id = user_info.get("oid", "anonymous")
                user_email = user_info.get("email", "no-email")

                file_data = {
                    'id': str(uuid.uuid4()),
                    'content': content,
                    'filename': filename,
                    'file_extension': file_extension,
                    'timestamp': str(datetime.datetime.utcnow()),
                    'user_id': user_id,
                    'user_email': user_email
                }
                cosmos_files_container.create_item(body=file_data)
                
                session['body'] = content
                session['topic'] = extract_topic_from_content(content)
                
                flash("File uploaded successfully!", "success")
                return redirect(url_for('options'))
            except Exception as e:
                print(f"Error processing the uploaded file: {e}")
                flash("An error occurred while processing the uploaded file.", "danger")
                return redirect(request.url)
        else:
            flash("Invalid file type. Please upload a DOCX, PDF, TXT, or MD file.", "danger")
            return redirect(request.url)
    return render_template('upload_content.html')

def extract_topic_from_content(content):
    if content:
        return content.strip().split('.')[0]
    else:
        return "Untitled Topic"

@app.route('/results')
def results():
    topic = session.get('topic', '')
    body = session.get('body', '')

    if not body.strip():
        flash("No content available to process. Please provide or upload content.", "danger")
        return redirect(url_for('index'))

    # Prompt 1: Determine the name of the hardware
    system_prompt = "You are a technical assistant that identifies hardware from a given user manual text."
    user_prompt = f"From the following text, identify the name of the hardware or device being documented: {body[:4000]}"

    response = openai.ChatCompletion.create(
        engine=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        max_tokens=100,
        temperature=0.0
    )
    hardware_name = response['choices'][0]['message']['content'].strip()

    # Prompt 2: Generate a list of commands from the document
    system_prompt = "You are a technical assistant that extracts commands from a user manual."
    user_prompt = f"Extract all the commands mentioned in the text. Return them as a bullet list:\n{body[:6000]}"
    response = openai.ChatCompletion.create(
        engine=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        max_tokens=1500,
        temperature=0.0
    )
    commands_text = response['choices'][0]['message']['content'].strip()
    # The commands_text could be a list, parse or just keep as text. Assume it's one command per line.
    commands = [c.strip('- ').strip() for c in commands_text.split('\n') if c.strip()]

    # Prompt 3: Generate a JSON of the commands
    system_prompt = "You are a technical assistant that formats commands into a JSON structure."
    example_json = """
    {
    "name": "BX",
    "description": "Scan switchbox",
    "syntax": "xxBX",
    "parameters": {
        "xx": "Controller number (integer) from 0 to 255."
    },
    "example": {
        "command": "2BX",
        "description": "Causes controller 2 connected to a PZC-SB switchbox to scan channels.",
        "query": "2BX?",
        "response_description": "Reports which switchbox channels of controller 2 were connected to an actuator during the last scan, with the response format indicating which channels were connected.",
        "response_example": "2BX 38 indicates that channels 1, 2, and 5 were connected to an actuator (38 in decimal equals 21+22+25 in binary). Bit0 corresponds to channel 1 and bit7 to channel 8."
    },
    "errors": {
        "no_switchbox_connected": "Err. 227: Command not allowed."
    },
    "related_commands": {
        "MX": "Select switchbox channel",
        "ID": "Actuator description"
    }
    }
    """
    user_prompt = f"""
    Convert the following list of commands into a JSON array, where each command is an object with 'name', 'description', 'syntax', 'parameters', 'example', 'errors', and 'related commands'.
    Here is an example JSON structure to guide you:
    {example_json}

    Commands:
    {commands_text}
    """

    response = openai.ChatCompletion.create(
        engine=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        max_tokens=2000,
        temperature=0.0
    )
    commands_json = response['choices'][0]['message']['content'].strip()


    # Prompt 4: Generate a Python script that uses the JSON to execute the commands
    system_prompt = "You are a technical assistant that writes Python code."
    example_python_script = """
    import serial
    import time

    # Constants
    CONTROLLER_NUMBER = 2
    COMMAND_PREFIX = "BX"
    SERIAL_PORT = "/dev/ttyUSB0"  # Example serial port name, adjust to your setup
    BAUD_RATE = 9600  # Example baud rate, adjust to your controller's specification

    # Create a serial object
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)

    # Function to scan switchbox channels
    def scan_switchbox(controller_number):
        command = f"{controller_number:02d}{COMMAND_PREFIX}?".encode()
        ser.write(command)
        time.sleep(0.1)  # Wait a bit for the response
        response = ser.readline().decode().strip()
        return response

    # Function to parse the response
    def parse_response(response):
        if response.startswith(f"{CONTROLLER_NUMBER:02d}{COMMAND_PREFIX}"):
            channels = int(response.split()[1])
            connected_channels = []
            for i in range(8):
                if channels & (1 << i):
                    connected_channels.append(i + 1)
            return connected_channels
        elif "Err. 227" in response:
            raise ValueError("No switchbox connected or command not allowed.")
        else:
            raise ValueError("Unknown response.")

    # Example usage
    try:
        response = scan_switchbox(CONTROLLER_NUMBER)
        connected_channels = parse_response(response)
        print(f"Connected channels: {connected_channels}")
    except ValueError as e:
        print(f"An error occurred: {e}")
    finally:
        ser.close()
    """

    user_prompt = f"""
    Write a Python script that uses the logic of the JSON to execute the command. Do not import the JSON directly, but use the structure to guide your script.

    JSON:
    {commands_json}

    Examples:
    Here is an example JSON structure to guide you:
    {example_json}

    Here is an example Python script to provide guidance:
    {example_python_script}
    """

    response = openai.ChatCompletion.create(
        engine=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        max_tokens=2000,
        temperature=0.0
    )
    python_script = response['choices'][0]['message']['content'].strip()


    results_data = {
        'hardware_name': hardware_name,
        'commands_list': commands,
        'commands_json': commands_json,
        'python_script': python_script
    }

    result_id = save_results_to_cosmos(results_data, topic, body)

    return render_template('results.html', results=results_data, topic=topic, body=body, result_id=result_id)


@app.route('/download_json/<result_id>')
def download_json(result_id):
    try:
        result = cosmos_results_container.read_item(item=result_id, partition_key=result_id)
        commands_json = result['results'].get('commands_json', '')
        if not commands_json:
            flash("No JSON available for download.", "danger")
            return redirect(url_for('view_result', result_id=result_id))

        data_io = BytesIO()
        data_io.write(commands_json.encode('utf-8'))
        data_io.seek(0)
        
        return send_file(
            data_io,
            mimetype='application/json',
            as_attachment=True,
            download_name='commands.json'
        )
    except Exception as e:
        print(f"Error during JSON download: {e}")
        flash("An error occurred while downloading the JSON.", "danger")
        return redirect(url_for('view_result', result_id=result_id))


@app.route('/download_python/<result_id>')
def download_python(result_id):
    try:
        result = cosmos_results_container.read_item(item=result_id, partition_key=result_id)
        python_script = result['results'].get('python_script', '')
        if not python_script:
            flash("No Python script available for download.", "danger")
            return redirect(url_for('view_result', result_id=result_id))

        data_io = BytesIO()
        data_io.write(python_script.encode('utf-8'))
        data_io.seek(0)
        
        return send_file(
            data_io,
            mimetype='text/x-python',
            as_attachment=True,
            download_name='commands.py'
        )
    except Exception as e:
        print(f"Error during Python script download: {e}")
        flash("An error occurred while downloading the Python script.", "danger")
        return redirect(url_for('view_result', result_id=result_id))


if __name__ == '__main__':
    app.run(debug=True)
