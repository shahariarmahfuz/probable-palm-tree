# -*- coding: utf-8 -*-
import os
import google.generativeai as genai
from flask import Flask, render_template, request, redirect, url_for, session, flash, g
import json
import psycopg2
import psycopg2.extras
import time
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_very_secret_dev_key_please_change')

DATABASE_URL = os.environ.get('DATABASE_URL', "postgresql://halabro_user:8A4vmzor3nbhhUApV5UGqXBsX7tEoBD3@dpg-d0aakis9c44c738qiq70-a.singapore-postgres.render.com/halabro")

gemini_model = None
try:
    # Ensure you have your actual API key set as an environment variable
    # or replace "YOUR_GEMINI_API_KEY" securely if not using environment variables.
    # Avoid hardcoding keys directly in the source code for security.
    # GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "YOUR_GEMINI_API_KEY")
    GOOGLE_API_KEY="AIzaSyDSYXJhANmrunwsV1ngTcTQULGh3Fhonnk" # Warning: Hardcoded key
    if GOOGLE_API_KEY and GOOGLE_API_KEY != "YOUR_GEMINI_API_KEY":
        genai.configure(api_key=GOOGLE_API_KEY)
        gemini_model = genai.GenerativeModel('gemini-1.5-flash')
    else:
        print("Warning: Gemini API key not found or is placeholder.")
        gemini_model = None
except Exception as e:
    print(f"Error configuring Gemini: {e}")
    gemini_model = None

def get_db():
    db = getattr(g, '_database', None)
    # Check if connection is closed (explicitly closed or lost)
    # db.closed != 0 means it's closed (0 means open)
    if db is None or db.closed != 0:
        try:
            db = g._database = psycopg2.connect(DATABASE_URL)
        except psycopg2.OperationalError as e:
            flash(f"ডাটাবেস সংযোগ স্থাপন করা যায়নি: {e}", "error")
            # Reraise the exception so the app knows connection failed
            raise e
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None and db.closed == 0: # Only close if open
        db.close()

# Helper function to check if a column exists in a table
def column_exists(db_connection, table_name, column_name):
    """Checks if a specific column exists in a given table."""
    with db_connection.cursor() as cursor:
        try:
            cursor.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'  -- Assuming public schema
                  AND table_name = %s
                  AND column_name = %s
            """, (table_name, column_name))
            return cursor.fetchone() is not None
        except psycopg2.Error as e:
            # Log or handle the error appropriately if needed
            print(f"Error checking column existence: {e}")
            return False # Assume it doesn't exist or error occurred

def init_db():
    """Initializes the database schema."""
    try:
        with app.app_context(): # Ensure we have application context
            db = get_db()
            with db.cursor() as cursor:
                # Create contents table if it doesn't exist
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS contents (
                        id SERIAL PRIMARY KEY,
                        title TEXT NOT NULL,
                        content_type TEXT NOT NULL CHECK(content_type IN ('গল্প', 'কবিতা', 'নাটক')),
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                # Create content_pages table if it doesn't exist
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS content_pages (
                        id SERIAL PRIMARY KEY,
                        content_id INTEGER NOT NULL,
                        page_number INTEGER NOT NULL,
                        page_content TEXT NOT NULL,
                        definitions TEXT, -- Store definitions as JSON string
                        important_lines TEXT, -- Store important lines as JSON string list
                        FOREIGN KEY (content_id) REFERENCES contents (id) ON DELETE CASCADE,
                        UNIQUE (content_id, page_number)
                    )
                ''')
                # Add 'important_lines' column if it doesn't exist (for backward compatibility)
                if not column_exists(db, 'content_pages', 'important_lines'):
                    print("Adding missing column 'important_lines' to 'content_pages'.")
                    cursor.execute('ALTER TABLE content_pages ADD COLUMN important_lines TEXT')

            db.commit() # Commit changes
            print("Database schema checked/initialized.")
    except (psycopg2.Error, Exception) as e:
        print(f"Error during DB initialization: {e}")
        # Attempt to rollback if an error occurred during transaction
        try:
            if 'db' in locals() and db and not db.closed:
                db.rollback()
        except Exception as rollback_e:
            print(f"Error during rollback attempt: {rollback_e}")
        # It might be appropriate to re-raise the exception or exit
        # depending on how critical the DB initialization is.
        # For now, just printing the error.

# Define CLI command to initialize DB
@app.cli.command('init-db')
def init_db_command():
    """Flask CLI command to initialize the database."""
    init_db()
    print('Initialized the PostgreSQL database.')

# --- Admin Credentials ---
# Use environment variables for credentials in production
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', "admin")
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', "password123") # Default for development

# --- Gemini AI Analysis Functions ---

# MODIFIED: Added content_type parameter and specific prompts
def get_difficult_words_and_definitions(text, content_type):
    """
    Identifies difficult words in Bengali text and provides definitions using Gemini AI.
    Uses different prompts based on content type (prose vs. poetry).

    Args:
        text (str): The Bengali text snippet to analyze.
        content_type (str): The type of content ('গল্প', 'নাটক', 'কবিতা').

    Returns:
        dict: A dictionary where keys are difficult words and values are definitions,
              or an empty dict if none found, or a dict with an 'error' key on failure.
    """
    if not gemini_model: return {"error": "Gemini model not initialized."}
    if not text or not text.strip(): return {}
    if not content_type: return {"error": "Content type is required for analysis."}

    prose_example_input = "তাহার পর দিবস উষাকালে কাঞ্চনমালা রাজপুত্রকে জাগরিত করিলেন।"
    prose_example_output_str = '{"দিবস": "দিন", "উষাকালে": "ভোরে", "জাগরিত": "জাগ্রত বা জাগানো"}'
    poem_example_input = "হে রুদ্র বৈশাখ, হে ভয়ংকর,\nতাপস নিঃশ্বাস বায়ে মুমূর্ষুরে দাও উড়ায়ে,\nবৎসরের আবর্জনা দূর হয়ে যাক যাক।"
    poem_example_output_str = '{"রুদ্র": "ভয়ংকর রূপ", "তাপস": "তপস্যাকারী", "নিঃশ্বাস": "শ্বাস", "বায়ে": "বাতাসে", "মুমূর্ষু": "মরণাপন্ন", "আবর্জনা": "ময়লা"}'

    # Select prompt based on content type
    if content_type in ['গল্প', 'নাটক']:
        prompt = f"""
        Analyze the following Bengali prose text snippet ({content_type}).
        Identify words that are generally difficult or less common in standard Bengali prose.
        Provide a simple, clear Bengali definition for each identified word.

        Input Text:
        \"\"\"
        {text}
        \"\"\"

        Your output MUST be a valid JSON object. Keys should be the identified difficult words, and values should be their simple Bengali definitions. Use double quotes (") for all keys and string values. Strictly avoid single quotes ('). If no applicable words are found, return an empty JSON object: {{}}.

        Example Input (Prose): "{prose_example_input}"
        Example JSON Output (Prose): {prose_example_output_str}

        Provide ONLY the JSON object as the output.
        """
    elif content_type == 'কবিতা':
        prompt = f"""
        Analyze the following Bengali poetry text snippet.
        Identify words that fit any of these categories:
        1.  **Archaic/Literary:** Words not commonly used in modern speech.
        2.  **Figurative Usage:** Common words used metaphorically.
        3.  **Less Common:** Words generally understood but not frequently encountered.
        4.  **Context-Critical:** Words whose specific meaning is essential to understanding.
        Provide a simple, clear Bengali definition for each identified word.

        Input Text:
        \"\"\"
        {text}
        \"\"\"

        Your output MUST be a valid JSON object. Keys should be the identified words, and values should be their simple Bengali definitions. Use double quotes (") for all keys and string values. Strictly avoid single quotes ('). If no applicable words are found, return an empty JSON object: {{}}.

        Example Input (Poetry): "{poem_example_input}"
        Example JSON Output (Poetry): {poem_example_output_str}

        Provide ONLY the JSON object as the output.
        """
    else:
        return {"error": f"Unsupported content type for analysis: {content_type}"}

    try:
        response = gemini_model.generate_content(prompt)
        # Attempt to extract JSON robustly
        raw_response_text = response.text
        response_text = raw_response_text.strip()

        # Find the outermost '{' and '}'
        json_start = response_text.find('{')
        json_end = response_text.rfind('}')

        if json_start != -1 and json_end != -1 and json_start < json_end:
            potential_json = response_text[json_start:json_end+1]
            try:
                definitions = json.loads(potential_json)
                if isinstance(definitions, dict):
                    return definitions
                else:
                    # AI returned something JSON-like but not an object
                    print(f"AI Warning (Definitions): Expected JSON object, got {type(definitions)}. Raw: {raw_response_text}")
                    return {"error": "AI returned unexpected data format (not a JSON object)."}
            except json.JSONDecodeError as json_err:
                # Log the problematic JSON string and the error
                print(f"AI JSON Decode Error (Definitions): {json_err}. Raw: {raw_response_text}. Attempted JSON: {potential_json}")
                return {"error": "Formatting error in AI response JSON."}
        elif response_text == '{}' or response_text == '{{}}' or not response_text:
             # Handle explicitly empty responses
             return {}
        else:
            # AI response didn't contain expected JSON structure
            print(f"AI Warning (Definitions): Could not find valid JSON object in response. Raw: {raw_response_text}")
            return {"error": "AI response did not contain a clearly identifiable JSON object."}
    except Exception as e:
        # Catch other potential errors (API call issues, etc.)
        print(f"AI Error (Definitions): Exception during Gemini call: {e}")
        return {"error": f"Problem communicating with AI: {e}"}

# MODIFIED: Added content_type parameter and specific prompts
def get_important_lines(text, content_type):
    """
    Identifies the most significant lines/phrases in Bengali text using Gemini AI.
    Uses different prompts based on content type (prose vs. poetry).

    Args:
        text (str): The Bengali text snippet to analyze.
        content_type (str): The type of content ('গল্প', 'নাটক', 'কবিতা').

    Returns:
        list: A list of strings containing the important lines/phrases,
              or an empty list if none found, or a dict with an 'error' key on failure.
    """
    if not gemini_model: return {"error": "Gemini model not initialized."}
    if not text or not text.strip(): return []
    if not content_type: return {"error": "Content type is required for analysis."}

    prose_example_input_imp = "রাজপুত্র এক গভীর জঙ্গলে প্রবেশ করল। সেখানে সে এক অদ্ভুত প্রাণীর সাক্ষাৎ পেল। প্রাণীটি কথা বলতে পারত এবং রাজপুত্রকে একটি ধাঁধার সমাধান করতে বলল। রাজপুত্র বুদ্ধিমত্তার সাথে ধাঁধার সমাধান করল।"
    prose_example_output_list_str_imp = '["রাজপুত্র এক গভীর জঙ্গলে প্রবেশ করে এক অদ্ভুত প্রাণীর সাক্ষাৎ পেল।", "প্রাণীটি রাজপুত্রকে একটি ধাঁধার সমাধান করতে বলে।", "রাজপুত্র বুদ্ধিমত্তার সাথে ধাঁধার সমাধান করে।"]'
    poem_example_input_imp = "স্বাধীনতা তুমি,\nরবি ঠাকুরের অজর কবিতা, অবিনাশী গান।\nস্বাধীনতা তুমি,\nকাজী নজরুল ঝাঁকড়া চুলের বাবরি দোলানো\nমহান পুরুষ, সৃষ্টি সুখের উল্লাসে কাঁপা-"
    poem_example_output_list_str_imp = '["স্বাধীনতা তুমি, রবি ঠাকুরের অজর কবিতা, অবিনাশী গান।", "স্বাধীনতা তুমি, কাজী নজরুল ঝাঁকড়া চুলের বাবরি দোলানো মহান পুরুষ"]'

    # Select prompt based on content type
    if content_type in ['গল্প', 'নাটক']:
        prompt = f"""
        Analyze the following Bengali prose text snippet ({content_type}).
        Identify the 2-3 most significant sentences or phrases that summarize the key actions, plot points, or main information presented in this specific snippet.

        Input Text:
        \"\"\"
        {text}
        \"\"\"

        Your output MUST be a valid JSON list of strings. Each string should be one selected sentence or phrase exactly as it appears or closely paraphrased if necessary for conciseness. Use double quotes (") for all strings within the list. Strictly avoid single quotes ('). If no truly significant lines can be identified from this snippet, return an empty JSON list: [].

        Example Input (Prose): "{prose_example_input_imp}"
        Example JSON Output (Prose): {prose_example_output_list_str_imp}

        Provide ONLY the JSON list as the output.
        """
    elif content_type == 'কবিতা':
        prompt = f"""
        Analyze the following Bengali poetry text snippet.
        Identify the 2-3 most significant lines or phrases that best capture or represent the:
        * Central theme or core message of this snippet.
        * Dominant mood or feeling evoked.
        * Most striking imagery used.
        * A particularly memorable or impactful statement within the snippet.

        Input Text:
        \"\"\"
        {text}
        \"\"\"

        Your output MUST be a valid JSON list of strings. Each string should be one selected line or phrase exactly as it appears in the text. Use double quotes (") for all strings within the list. Strictly avoid single quotes ('). If no truly significant lines can be identified from this snippet, return an empty JSON list: [].

        Example Input (Poetry): "{poem_example_input_imp}"
        Example JSON Output (Poetry): {poem_example_output_list_str_imp}

        Provide ONLY the JSON list as the output.
        """
    else:
        return {"error": f"Unsupported content type for analysis: {content_type}"}

    try:
        response = gemini_model.generate_content(prompt)
        # Attempt to extract JSON list robustly
        raw_response_text = response.text
        response_text = raw_response_text.strip()

        # Find the outermost '[' and ']'
        json_start = response_text.find('[')
        json_end = response_text.rfind(']')

        if json_start != -1 and json_end != -1 and json_start < json_end:
            potential_json_list = response_text[json_start:json_end+1]
            try:
                important_lines = json.loads(potential_json_list)
                # Validate that it's a list and all elements are strings
                if isinstance(important_lines, list) and all(isinstance(item, str) for item in important_lines):
                    return important_lines
                else:
                    # AI returned JSON but not a list of strings
                    print(f"AI Warning (Imp. Lines): Expected JSON list of strings, got {type(important_lines)}. Raw: {raw_response_text}")
                    return {"error": "AI did not return a valid list of strings."}
            except json.JSONDecodeError as json_err:
                # Log the problematic JSON string and the error
                print(f"AI JSON Decode Error (Imp. Lines): {json_err}. Raw: {raw_response_text}. Attempted JSON: {potential_json_list}")
                return {"error": "Formatting error in AI list response."}
        elif response_text == '[]' or not response_text:
             # Handle explicitly empty responses
            return []
        else:
            # AI response didn't contain expected JSON list structure
            print(f"AI Warning (Imp. Lines): Could not find valid JSON list in response. Raw: {raw_response_text}")
            return {"error": "AI response was not in the expected JSON list format."}
    except Exception as e:
        # Catch other potential errors (API call issues, etc.)
        print(f"AI Error (Imp. Lines): Exception during Gemini call: {e}")
        return {"error": f"Problem communicating with AI: {e}"}

# --- Jinja Filter ---
def format_datetime_filter(value, format='%Y-%m-%d %H:%M:%S'):
    """Jinja filter to format datetime objects."""
    if isinstance(value, datetime):
        return value.strftime(format)
    return value if value else ""

app.jinja_env.filters['datetimeformat'] = format_datetime_filter

# --- Flask Routes ---

@app.route('/')
def index():
    """Displays the homepage with content grouped by type."""
    grouped_content = {}
    try:
        db = get_db()
        with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            cursor.execute('SELECT id, title, content_type FROM contents ORDER BY created_at DESC')
            all_content = cursor.fetchall()

        # Group content by type
        for item in all_content:
            ctype = item['content_type']
            if ctype not in grouped_content:
                grouped_content[ctype] = []
            grouped_content[ctype].append(item)

    except (Exception, psycopg2.Error) as e:
        flash(f"কনটেন্ট লোড করতে সমস্যা হয়েছে: {e}", "error")
        # Log the error for debugging
        print(f"Error loading content for index: {e}")

    return render_template('index.html', grouped_content=grouped_content)

@app.route('/content/<int:content_id>')
def view_content_first_page(content_id):
    """Redirects to the first page of a specific content item."""
    first_page_number = None
    try:
        db = get_db()
        with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            # Find the minimum page number for the given content_id
            cursor.execute('SELECT MIN(page_number) as first_page FROM content_pages WHERE content_id = %s', (content_id,))
            result = cursor.fetchone()
            if result and result['first_page'] is not None:
                 first_page_number = result['first_page']
            else:
                # Check if the content item itself exists even if it has no pages
                cursor.execute('SELECT id FROM contents WHERE id = %s', (content_id,))
                content_exists = cursor.fetchone()
                if not content_exists:
                    flash(f"ID {content_id} সহ কনটেন্ট খুঁজে পাওয়া যায়নি।", "error")
                    return redirect(url_for('index'))
                # Content exists but has no pages
                flash(f"এই কনটেন্ট ({content_id}) এ কোনো পাতা যোগ করা হয়নি।", "warning")
                # Redirect to index or maybe an admin page if appropriate
                return redirect(url_for('index'))

    except (Exception, psycopg2.Error) as e:
        flash(f"প্রথম পাতা খুঁজতে সমস্যা হয়েছে: {e}", "error")
        print(f"Error finding first page for content {content_id}: {e}")
        return redirect(url_for('index'))

    # Redirect to the specific page view if a first page number was found
    return redirect(url_for('view_content_page', content_id=content_id, page_number=first_page_number))


@app.route('/content/<int:content_id>/page/<int:page_number>')
def view_content_page(content_id, page_number):
    """Displays a specific page of a content item."""
    content_item = None
    page_data = None
    total_pages = 0
    definitions = {}
    important_lines = []

    try:
        db = get_db()
        with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            # Get content metadata (title, type)
            cursor.execute('SELECT id, title, content_type FROM contents WHERE id = %s', (content_id,))
            content_item = cursor.fetchone()
            if not content_item:
                flash(f"ID {content_id} সহ কনটেন্ট খুঁজে পাওয়া যায়নি।", "error")
                return redirect(url_for('index'))

            # Get the specific page data
            cursor.execute('''
                SELECT page_number, page_content, definitions, important_lines
                FROM content_pages
                WHERE content_id = %s AND page_number = %s
            ''', (content_id, page_number))
            page_data = cursor.fetchone()

            if not page_data:
                # Check if *any* pages exist for this content to give a better error
                cursor.execute('SELECT page_number FROM content_pages WHERE content_id = %s ORDER BY page_number ASC LIMIT 1', (content_id,))
                first_page_res = cursor.fetchone()
                if first_page_res:
                    flash(f"'{content_item['title']}' এর {page_number} নম্বর পাতা খুঁজে পাওয়া যায়নি। প্রথম পাতায় পাঠানো হচ্ছে।", "warning")
                    return redirect(url_for('view_content_page', content_id=content_id, page_number=first_page_res['page_number']))
                else:
                    # Content exists, but no pages at all
                    flash(f"'{content_item['title']}' এর জন্য কোনো পাতা যোগ করা হয়নি।", "error")
                    return redirect(url_for('index'))

            # Get total number of pages for pagination
            cursor.execute('SELECT COUNT(id) FROM content_pages WHERE content_id = %s', (content_id,))
            total_pages = cursor.fetchone()[0]

            # Safely parse JSON data from the database
            definitions_json = page_data['definitions']
            if definitions_json:
                try:
                    definitions = json.loads(definitions_json)
                    if not isinstance(definitions, dict):
                        print(f"Warning: Definitions for content {content_id}, page {page_number} is not a dict: {definitions_json}")
                        definitions = {} # Reset to empty dict if format is wrong
                except json.JSONDecodeError as e:
                    print(f"Error decoding definitions JSON for content {content_id}, page {page_number}: {e}. JSON: {definitions_json}")
                    definitions = {"error": "সংজ্ঞা লোড করতে সমস্যা হয়েছে"}

            important_lines_json = page_data['important_lines']
            if important_lines_json:
                try:
                    important_lines = json.loads(important_lines_json)
                    if not isinstance(important_lines, list):
                         print(f"Warning: Important lines for content {content_id}, page {page_number} is not a list: {important_lines_json}")
                         important_lines = [] # Reset to empty list if format is wrong
                except json.JSONDecodeError as e:
                    print(f"Error decoding important lines JSON for content {content_id}, page {page_number}: {e}. JSON: {important_lines_json}")
                    important_lines = ["গুরুত্বপূর্ণ লাইন লোড করতে সমস্যা হয়েছে"]

    except (Exception, psycopg2.Error) as e:
        flash(f"পাতা ({page_number}) লোড করার সময় একটি ত্রুটি ঘটেছে: {e}", "error")
        print(f"Error loading page {page_number} for content {content_id}: {e}")
        # Redirect to index as a fallback
        return redirect(url_for('index'))

    return render_template('content_page.html',
                         content_item=content_item,
                         page_data=page_data,
                         definitions=definitions,
                         important_lines=important_lines,
                         current_page=page_number,
                         total_pages=total_pages,
                         content_id=content_id)

# --- Admin Routes ---

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    """Handles admin login."""
    if 'admin_logged_in' in session:
        return redirect(url_for('admin_dashboard')) # Already logged in

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        # Simple comparison (consider using password hashing in production)
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['admin_logged_in'] = True
            session.permanent = True # Make session last longer
            flash("সফলভাবে লগইন করেছেন!", "success")
            return redirect(url_for('admin_dashboard'))
        else:
            flash("ভুল ইউজারনেম বা পাসওয়ার্ড।", "error")
            # Optional: Add a small delay or log failed attempts to deter brute-force

    return render_template('admin_login.html')

@app.route('/admin/logout')
def admin_logout():
    """Logs the admin out."""
    session.pop('admin_logged_in', None)
    flash("সফলভাবে লগআউট হয়েছেন।", "info")
    return redirect(url_for('admin_login'))

@app.route('/admin/dashboard')
def admin_dashboard():
    """Displays the admin dashboard with a list of content."""
    if 'admin_logged_in' not in session:
        flash("ড্যাশবোর্ড দেখতে অনুগ্রহ করে লগইন করুন।", "warning")
        return redirect(url_for('admin_login'))

    all_content = []
    try:
        db = get_db()
        with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            # Query to get content list with page count
            cursor.execute('''
                SELECT
                    c.id,
                    c.title,
                    c.content_type,
                    c.created_at,
                    COUNT(p.id)::int as page_count
                FROM contents c
                LEFT JOIN content_pages p ON c.id = p.content_id
                GROUP BY c.id, c.title, c.content_type, c.created_at
                ORDER BY c.created_at DESC
            ''')
            all_content = cursor.fetchall()
    except (Exception, psycopg2.Error) as e:
        flash(f"ড্যাশবোর্ডের জন্য কনটেন্ট লোড করতে সমস্যা: {e}", "error")
        print(f"Error loading admin dashboard content: {e}")

    return render_template('admin_dashboard.html', contents=all_content)

@app.route('/admin/add_content', methods=['POST'])
def add_content():
    """Handles adding new content and its pages, including AI analysis."""
    if 'admin_logged_in' not in session:
        flash("কনটেন্ট যোগ করতে লগইন করুন।", "warning")
        return redirect(url_for('admin_login'))

    title = request.form.get('title', '').strip()
    content_type = request.form.get('content_type')
    # Get list of page contents, filter out empty ones
    pages_content = request.form.getlist('pages[]') # Assuming 'pages[]' is the name in the form
    valid_pages_content = [p.strip() for p in pages_content if p and p.strip()] # Ensure page content is not just whitespace

    # --- Input Validation ---
    if not title:
        flash("শিরোনাম আবশ্যক।", "error")
        return redirect(url_for('admin_dashboard'))
    if not content_type or content_type not in ['গল্প', 'কবিতা', 'নাটক']:
        flash("সঠিক কনটেন্ট টাইপ ('গল্প', 'কবিতা', বা 'নাটক') নির্বাচন করুন।", "error")
        return redirect(url_for('admin_dashboard'))
    if not valid_pages_content:
        flash("অন্তত একটি পাতায় লেখা থাকতে হবে।", "error")
        return redirect(url_for('admin_dashboard'))

    db = get_db()
    cursor = None # Initialize cursor to None
    new_content_id = None
    ai_def_errors = [] # Pages where definition AI failed
    ai_imp_errors = [] # Pages where important lines AI failed
    total_pages_processed = 0
    process_successful = True # Flag to track overall success

    try:
        cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor) # Use DictCursor for fetching 'id'

        # 1. Insert into contents table and get the new ID
        cursor.execute(
            'INSERT INTO contents (title, content_type) VALUES (%s, %s) RETURNING id',
            (title, content_type)
        )
        result = cursor.fetchone()
        if not result or 'id' not in result:
            # This should ideally not happen if RETURNING id works
            db.rollback() # Rollback the transaction
            raise psycopg2.DatabaseError("কনটেন্ট হেডার যোগ করে আইডি পেতে ব্যর্থ হয়েছে।")
        new_content_id = result['id']

        # 2. Process and insert each page
        num_pages = len(valid_pages_content)
        for i, page_text in enumerate(valid_pages_content):
            page_number = i + 1
            print(f"Processing page {page_number}/{num_pages} for content '{title}' (ID: {new_content_id})")

            # Rate limiting: Add delay between AI calls if processing multiple pages
            # Adjust sleep time as needed based on API limits/performance
            # No delay needed before the first page (i=0)
            if i > 0 and gemini_model:
                print("Waiting 10 seconds before next AI call...")
                time.sleep(10) # Wait 10 seconds

            # --- Call AI for definitions (pass content_type) ---
            definitions_result = {}
            if gemini_model:
                # MODIFIED: Pass content_type here
                definitions_result = get_difficult_words_and_definitions(page_text, content_type)
            else:
                definitions_result = {"error": "AI model not available"}

            def_dict = {}
            if isinstance(definitions_result, dict) and "error" not in definitions_result:
                def_dict = definitions_result
            else: # Handle error case or non-dict result
                ai_def_errors.append(page_number)
                error_msg = definitions_result.get("error", "Unknown AI definitions error") if isinstance(definitions_result, dict) else "Invalid AI response type"
                print(f"AI Error (Definitions) for page {page_number}: {error_msg}")


            # --- Call AI for important lines (pass content_type) ---
            important_lines_result = []
            if gemini_model:
                 # MODIFIED: Pass content_type here
                important_lines_result = get_important_lines(page_text, content_type)
            else:
                important_lines_result = {"error": "AI model not available"}

            imp_lines_list = []
            if isinstance(important_lines_result, list):
                 imp_lines_list = important_lines_result
            else: # Handle error case or non-list result
                ai_imp_errors.append(page_number)
                error_msg = important_lines_result.get("error", "Unknown AI important lines error") if isinstance(important_lines_result, dict) else "Invalid AI response type"
                print(f"AI Error (Imp. Lines) for page {page_number}: {error_msg}")

            # Convert results to JSON strings for DB storage (use None if empty/error)
            definitions_json = json.dumps(def_dict, ensure_ascii=False) if def_dict else None
            important_lines_json = json.dumps(imp_lines_list, ensure_ascii=False) if imp_lines_list else None

            # 3. Insert into content_pages table
            cursor.execute(
                '''INSERT INTO content_pages
                   (content_id, page_number, page_content, definitions, important_lines)
                   VALUES (%s, %s, %s, %s, %s)''',
                (new_content_id, page_number, page_text, definitions_json, important_lines_json)
            )
            total_pages_processed += 1

        # 4. Commit the transaction if all pages were inserted successfully
        db.commit()
        print(f"Successfully committed content '{title}' and {total_pages_processed} pages.")

        # --- Flash message based on AI results ---
        if not ai_def_errors and not ai_imp_errors:
            flash(f"'{title}' ({content_type}) সফলভাবে যোগ করা হয়েছে ({total_pages_processed} পাতা) এবং AI ডেটা সফলভাবে জেনারেট ও সংরক্ষণ করা হয়েছে।", "success")
        else:
            error_parts = []
            if ai_def_errors:
                error_parts.append(f"পাতা {', '.join(map(str, ai_def_errors))} এর জন্য কঠিন শব্দের অর্থ")
            if ai_imp_errors:
                 error_parts.append(f"পাতা {', '.join(map(str, ai_imp_errors))} এর জন্য গুরুত্বপূর্ণ লাইন")
            flash(f"'{title}' ({content_type}) ({total_pages_processed} পাতা) যোগ করা হয়েছে, কিন্তু {' ও '.join(error_parts)} তৈরি করতে সমস্যা হয়েছে। অনুগ্রহ করে পরে 'রি-এনালাইজ' করার চেষ্টা করুন।", "warning")

    except (Exception, psycopg2.Error) as e:
        process_successful = False
        print(f"Error adding content '{title}': {e}")
        flash(f"কনটেন্ট '{title}' যোগ করার সময় একটি মারাত্মক ত্রুটি ঘটেছে: {e}", "error")
        # Rollback the transaction in case of any error during the process
        if db and not db.closed:
             try:
                 db.rollback()
                 print("Transaction rolled back due to error.")
             except Exception as rollback_e:
                 print(f"Error during rollback: {rollback_e}")
        # Redirect back even on error
        return redirect(url_for('admin_dashboard'))
    finally:
        # Ensure cursor is closed if it was opened
        if cursor:
            cursor.close()

    # Redirect after successful completion (or if error handled by flash message)
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/edit_content/<int:content_id>', methods=['GET'])
def edit_content_form(content_id):
    """Displays the form to edit existing content."""
    if 'admin_logged_in' not in session:
        flash("কনটেন্ট সম্পাদনা করতে লগইন করুন।", "warning")
        return redirect(url_for('admin_login'))

    content_item = None
    pages = []
    try:
        db = get_db()
        with db.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            # Fetch content metadata
            cursor.execute('SELECT id, title, content_type FROM contents WHERE id = %s', (content_id,))
            content_item = cursor.fetchone()
            if not content_item:
                flash(f"ID {content_id} সহ কনটেন্ট খুঁজে পাওয়া যায়নি।", "error")
                return redirect(url_for('admin_dashboard'))

            # Fetch all pages associated with this content
            cursor.execute('''
                SELECT id, page_number, page_content
                FROM content_pages
                WHERE content_id = %s
                ORDER BY page_number ASC
            ''', (content_id,))
            pages = cursor.fetchall()
            # It's okay if pages list is empty, maybe the user wants to add pages later

    except (Exception, psycopg2.Error) as e:
        flash(f"সম্পাদনার জন্য কনটেন্ট লোড করতে সমস্যা: {e}", "error")
        print(f"Error loading content {content_id} for edit: {e}")
        return redirect(url_for('admin_dashboard'))

    return render_template('edit_content.html', content_item=content_item, pages=pages)

@app.route('/admin/edit_content/<int:content_id>', methods=['POST'])
def update_content(content_id):
    """Handles updating existing content metadata and page text."""
    if 'admin_logged_in' not in session:
        flash("কনটেন্ট সম্পাদনা করতে লগইন করুন।", "warning")
        return redirect(url_for('admin_login'))

    title = request.form.get('title', '').strip()
    content_type = request.form.get('content_type')
    # Get page content and corresponding IDs from the form
    pages_content = request.form.getlist('pages_content[]') # Content from textareas
    page_ids = request.form.getlist('page_ids[]')           # Hidden input with page IDs

    # --- Input Validation ---
    if not title:
        flash("শিরোনাম আবশ্যক।", "error")
        # Redirect back to the edit form for the same content ID
        return redirect(url_for('edit_content_form', content_id=content_id))
    if not content_type or content_type not in ['গল্প', 'কবিতা', 'নাটক']:
        flash("সঠিক কনটেন্ট টাইপ ('গল্প', 'কবিতা', বা 'নাটক') নির্বাচন করুন।", "error")
        return redirect(url_for('edit_content_form', content_id=content_id))
    if len(pages_content) != len(page_ids):
        flash("পেজ ডেটা অসামঞ্জস্যপূর্ণ। প্রতিটি পেজের কন্টেন্ট এবং আইডি প্রয়োজন।", "error")
        print(f"Error: Mismatch between page content count ({len(pages_content)}) and page ID count ({len(page_ids)}) for content {content_id}")
        return redirect(url_for('edit_content_form', content_id=content_id))

    db = get_db()
    cursor = None # Initialize cursor
    updated_page_count = 0
    process_successful = True

    try:
        cursor = db.cursor() # No need for DictCursor here

        # 1. Update the main content entry (title, type)
        cursor.execute(
            'UPDATE contents SET title = %s, content_type = %s WHERE id = %s',
            (title, content_type, content_id)
        )
        if cursor.rowcount == 0:
             # This case should be rare if the edit form loaded correctly
             flash(f"আপডেট করার জন্য ID {content_id} সহ কনটেন্ট খুঁজে পাওয়া যায়নি।", "warning")
             # No need to rollback yet, maybe pages still exist? Proceed cautiously.

        # 2. Update each page's content
        for page_id, page_text in zip(page_ids, pages_content):
            # Consider adding validation for page_id (e.g., ensure it's an integer)
            try:
                current_page_id = int(page_id)
            except ValueError:
                print(f"Warning: Invalid page ID '{page_id}' skipped during update for content {content_id}.")
                continue # Skip this page update

            cleaned_content = page_text.strip()
            # Decide if you want to allow empty pages. If not, add: if cleaned_content:
            # For now, allowing update even if content is empty
            cursor.execute(
                # Ensure we only update pages belonging to the correct content_id
                'UPDATE content_pages SET page_content = %s WHERE id = %s AND content_id = %s',
                (cleaned_content, current_page_id, content_id)
            )
            # rowcount tells if the update actually happened (i.e., the page ID and content ID matched)
            if cursor.rowcount > 0:
                updated_page_count += 1
            else:
                print(f"Warning: Page ID {current_page_id} not found or doesn't belong to content ID {content_id}. No update performed for this page.")


        # 3. Commit the transaction
        db.commit()
        flash(f"'{title}' সফলভাবে সম্পাদন করা হয়েছে ({updated_page_count} টি পাতা আপডেট হয়েছে)। দ্রষ্টব্য: এই সম্পাদনা AI দ্বারা তৈরি ডেটা (শব্দার্থ, গুরুত্বপূর্ণ লাইন) আপডেট করে না। প্রয়োজন হলে অনুগ্রহ করে 'রি-এনালাইজ' করুন।", "success")

    except (Exception, psycopg2.Error) as e:
        process_successful = False
        print(f"Error updating content {content_id}: {e}")
        flash(f"কনটেন্ট '{title}' সম্পাদন করার সময় একটি ত্রুটি ঘটেছে: {e}", "error")
        # Rollback on error
        if db and not db.closed:
            try:
                db.rollback()
                print("Transaction rolled back due to update error.")
            except Exception as rollback_e:
                print(f"Error during rollback: {rollback_e}")
        # Redirect back to the edit form on error to allow correction
        return redirect(url_for('edit_content_form', content_id=content_id))
    finally:
        if cursor:
            cursor.close()

    # Redirect to dashboard after successful update
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/reanalyze/<int:content_id>', methods=['POST'])
def reanalyze_content(content_id):
    """Re-runs AI analysis (definitions, important lines) for all pages of a content item."""
    if 'admin_logged_in' not in session:
        flash("কনটেন্ট রি-এনালাইজ করতে লগইন করুন।", "warning")
        return redirect(url_for('admin_login'))

    db = get_db()
    read_cursor = None
    update_cursor = None

    ai_def_errors = []
    ai_imp_errors = []
    updated_pages_count = 0
    total_pages_to_analyze = 0
    content_title = f"ID {content_id}"
    content_type = None # MODIFIED: Need this for AI calls
    process_successful = True

    try:
        # Use DictCursor for reading
        read_cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
        update_cursor = db.cursor() # Standard cursor for updates

        # 1. Get content metadata (title and type)
        # MODIFIED: Fetch content_type as well
        read_cursor.execute('SELECT title, content_type FROM contents WHERE id = %s', (content_id,))
        content_meta = read_cursor.fetchone()
        if not content_meta:
            flash(f"রি-এনালাইজ করার জন্য ID {content_id} সহ কনটেন্ট খুঁজে পাওয়া যায়নি।", "error")
            process_successful = False # Stop processing
        else:
            content_title = content_meta['title']
            content_type = content_meta['content_type'] # MODIFIED: Store content_type
            if not content_type:
                 # This should not happen due to DB constraints, but check anyway
                 flash(f"'{content_title}' (ID: {content_id}) এর জন্য কনটেন্ট টাইপ পাওয়া যায়নি। রি-এনালাইসিস সম্ভব নয়।", "error")
                 process_successful = False

        # 2. Fetch all pages for this content if metadata was found
        pages_to_analyze = []
        if process_successful:
            read_cursor.execute('''
                SELECT id, page_number, page_content
                FROM content_pages
                WHERE content_id = %s
                ORDER BY page_number ASC
            ''', (content_id,))
            pages_to_analyze = read_cursor.fetchall()
            total_pages_to_analyze = len(pages_to_analyze)

            if not pages_to_analyze:
                flash(f"'{content_title}' এর জন্য কোনো পাতা পাওয়া যায়নি। রি-এনালাইজ করার কিছু নেই।", "warning")
                process_successful = False # Nothing to do

        # 3. Analyze each page if pages exist and prerequisites met
        if process_successful:
            print(f"Starting re-analysis for '{content_title}' ({content_type}), {total_pages_to_analyze} pages...")
            for i, page in enumerate(pages_to_analyze):
                page_id = page['id']
                page_num = page['page_number']
                page_content = page['page_content']

                print(f"Re-analyzing page {page_num}/{total_pages_to_analyze} (Page ID: {page_id})...")

                # Rate limiting between AI calls
                if i > 0 and gemini_model:
                    print("Waiting 10 seconds before next AI call...")
                    time.sleep(10)

                # --- Call AI for definitions ---
                definitions_result = {}
                if gemini_model:
                    # MODIFIED: Pass content_type
                    definitions_result = get_difficult_words_and_definitions(page_content, content_type)
                else:
                    definitions_result = {"error": "AI model not available"}

                def_dict = {}
                if isinstance(definitions_result, dict) and "error" not in definitions_result:
                    def_dict = definitions_result
                else:
                    ai_def_errors.append(page_num)
                    error_msg = definitions_result.get("error", "Unknown AI def error") if isinstance(definitions_result, dict) else "Invalid AI def type"
                    print(f"AI Error (Re-analyze Definitions) for page {page_num}: {error_msg}")

                # --- Call AI for important lines ---
                important_lines_result = []
                if gemini_model:
                    # MODIFIED: Pass content_type
                    important_lines_result = get_important_lines(page_content, content_type)
                else:
                    important_lines_result = {"error": "AI model not available"}

                imp_lines_list = []
                if isinstance(important_lines_result, list):
                    imp_lines_list = important_lines_result
                else:
                    ai_imp_errors.append(page_num)
                    error_msg = important_lines_result.get("error", "Unknown AI imp lines error") if isinstance(important_lines_result, dict) else "Invalid AI imp lines type"
                    print(f"AI Error (Re-analyze Imp. Lines) for page {page_num}: {error_msg}")

                # Convert to JSON
                definitions_json = json.dumps(def_dict, ensure_ascii=False) if def_dict else None
                important_lines_json = json.dumps(imp_lines_list, ensure_ascii=False) if imp_lines_list else None

                # --- Update the database ---
                # Use a separate transaction for each page update to save progress incrementally?
                # Or keep a single transaction for all pages?
                # Let's use a single transaction for now for simplicity.
                update_cursor.execute(
                    '''UPDATE content_pages
                       SET definitions = %s, important_lines = %s
                       WHERE id = %s''', # Can just use page id as it's primary key
                    (definitions_json, important_lines_json, page_id)
                )
                # Check if update actually occurred (rowcount > 0)
                if update_cursor.rowcount > 0:
                    updated_pages_count += 1
                else:
                     print(f"Warning: Update for page ID {page_id} (page {page_num}) resulted in 0 rows affected.")


            # 4. Commit all updates after the loop finishes
            db.commit()
            print(f"Re-analysis complete for '{content_title}'. Committed updates for {updated_pages_count} pages.")

            # --- Flash message based on results ---
            if updated_pages_count == total_pages_to_analyze:
                 if not ai_def_errors and not ai_imp_errors:
                     flash(f"'{content_title}' সফলভাবে রি-এনালাইজ করা হয়েছে ({updated_pages_count}/{total_pages_to_analyze} পাতা আপডেট)। AI ডেটা সফলভাবে জেনারেট করা হয়েছে।", "success")
                 else:
                     error_parts = []
                     if ai_def_errors: error_parts.append(f"পাতা {', '.join(map(str, ai_def_errors))} এর ডেফিনিশন")
                     if ai_imp_errors: error_parts.append(f"পাতা {', '.join(map(str, ai_imp_errors))} এর গুরুত্বপূর্ণ লাইন")
                     flash(f"'{content_title}' রি-এনালাইজ সম্পন্ন ({updated_pages_count}/{total_pages_to_analyze} পাতা আপডেট), কিন্তু {' ও '.join(error_parts)} আনতে সমস্যা হয়েছে।", "warning")
            else:
                 # Case where not all pages were updated (should be rare if selection was correct)
                 flash(f"'{content_title}' রি-এনালাইজ সম্পন্ন, কিন্তু শুধুমাত্র {updated_pages_count}/{total_pages_to_analyze} পাতা আপডেট করা সম্ভব হয়েছে। অনুগ্রহ করে লগ চেক করুন।", "warning")


    except (Exception, psycopg2.Error) as e:
        # Only rollback if an error occurred *during* the processing loop or commit
        if process_successful: # Check if error happened after initial checks
             db.rollback()
             print(f"Transaction rolled back due to re-analysis error for '{content_title}': {e}")
        flash(f"'{content_title}' রি-এনালাইজ করার সময় একটি মারাত্মক ত্রুটি ঘটেছে: {e}", "error")
        # Don't set process_successful = False here, it's already implied by exception
    finally:
        # Ensure cursors are closed
        if read_cursor: read_cursor.close()
        if update_cursor: update_cursor.close()

    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete_content/<int:content_id>', methods=['POST'])
def delete_content(content_id):
    """Deletes a content item and all its associated pages."""
    if 'admin_logged_in' not in session:
        flash("কনটেন্ট মুছতে লগইন করুন।", "warning")
        return redirect(url_for('admin_login'))

    db = get_db()
    read_cursor = None
    delete_cursor = None
    title_to_delete = f"ID {content_id}" # Default title if lookup fails
    deleted = False

    try:
        # Use DictCursor to fetch the title for the confirmation message
        read_cursor = db.cursor(cursor_factory=psycopg2.extras.DictCursor)
        delete_cursor = db.cursor() # Standard cursor for deletion

        # Optional: Get the title before deleting for a better message
        read_cursor.execute('SELECT title FROM contents WHERE id = %s', (content_id,))
        content_item = read_cursor.fetchone()
        if content_item:
            title_to_delete = content_item['title']
        else:
            # Content already doesn't exist
            flash(f"ID {content_id} সহ কোনো কনটেন্ট খুঁজে পাওয়া যায়নি। কিছুই মোছা হয়নি।", "warning")
            return redirect(url_for('admin_dashboard'))

        # Execute the delete operation.
        # FOREIGN KEY constraint with ON DELETE CASCADE in content_pages
        # should automatically delete associated pages.
        delete_cursor.execute('DELETE FROM contents WHERE id = %s', (content_id,))

        # Check if the deletion actually happened
        if delete_cursor.rowcount > 0:
            db.commit() # Commit the deletion
            flash(f"'{title_to_delete}' এবং এর সংশ্লিষ্ট সকল পাতা সফলভাবে মুছে ফেলা হয়েছে।", "success")
            deleted = True
            print(f"Content '{title_to_delete}' (ID: {content_id}) deleted successfully.")
        else:
            # This case should be covered by the initial check, but as a fallback
            db.rollback() # Rollback if delete didn't affect any rows
            flash(f"'{title_to_delete}' (ID: {content_id}) মোছার সময় কোনো রো প্রভাবিত হয়নি।", "error")

    except (Exception, psycopg2.Error) as e:
        # Rollback on any exception during the delete process
        if db and not db.closed:
            try:
                db.rollback()
                print(f"Transaction rolled back due to delete error for content {content_id}: {e}")
            except Exception as rollback_e:
                 print(f"Error during rollback: {rollback_e}")
        flash(f"কনটেন্ট '{title_to_delete}' মোছার সময় একটি ত্রুটি ঘটেছে: {e}", "error")
        print(f"Error deleting content {content_id}: {e}")
    finally:
        # Ensure cursors are closed
        if read_cursor: read_cursor.close()
        if delete_cursor: delete_cursor.close()

    return redirect(url_for('admin_dashboard'))

# --- Main Execution ---
if __name__ == '__main__':
    # Initialize DB schema on startup (optional, can be done via CLI)
    # Using app.app_context() ensures Flask context is available for get_db
    with app.app_context():
        init_db()

    # Run the Flask development server
    # Use 0.0.0.0 to make it accessible on the network
    # Set debug=False for production
    app.run(host="0.0.0.0", port=5000, debug=True) # debug=True enables auto-reloading and verbose errors
