import os
import sqlite3
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import anthropic
import PyPDF2
import mammoth

from dotenv import load_dotenv
import uuid
import json

load_dotenv(override=True)

# Anthropic API Key
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "Joeygogo123_fallback")

DATABASE = 'database.db'
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'txt', 'pdf', 'docx'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# PDF extraction
def extract_text_from_pdf(pdf_path):
    text = ""
    try:
        with open(pdf_path, 'rb') as file:
            reader = PyPDF2.PdfReader(file)
            for i, page in enumerate(reader.pages):
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
                else:
                    print(f"Page {i+1} did not extract words")
    except Exception as e:
        print("PDF extraction fail!", e)
    return text

# DOCX HTML extraction
def extract_html_from_docx(docx_path):
    html = ""
    try:
        with open(docx_path, "rb") as docx_file:
            result = mammoth.convert_to_html(docx_file)
            html = result.value
    except Exception as e:
        print("DOCX extraction fail!", e)
    return html

# Database Initialization

def init_db():
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        );
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS directories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            parent_id INTEGER,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (parent_id) REFERENCES directories(id)
        );
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            directory_id INTEGER,
            user_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            filepath TEXT NOT NULL,
            upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (directory_id) REFERENCES directories(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS saved_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            directory_id INTEGER,
            type TEXT NOT NULL,
            question TEXT NOT NULL,
            options TEXT,
            answer TEXT NOT NULL,
            explanation TEXT,
            saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (directory_id) REFERENCES directories(id)
        );
    ''')
    
    # 歷史紀錄資料表遷移
    try:
        cursor.execute("ALTER TABLE saved_questions ADD COLUMN directory_id INTEGER REFERENCES directories(id);")
    except sqlite3.OperationalError:
        pass # 代表已存在
        


    conn.commit()
    conn.close()

def migrate_db():
    conn = get_db_connection()
    users_with_orphans = conn.execute("SELECT DISTINCT user_id FROM files WHERE directory_id IS NULL").fetchall()
    for row in users_with_orphans:
        user_id = row['user_id']
        default_dir = conn.execute("SELECT id FROM directories WHERE user_id = ? AND name = ?", (user_id, '預設專案')).fetchone()
        if not default_dir:
            conn.execute("INSERT INTO directories (user_id, name) VALUES (?, ?)", (user_id, '預設專案'))
            default_dir_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        else:
            default_dir_id = default_dir['id']
        conn.execute("UPDATE files SET directory_id = ? WHERE directory_id IS NULL AND user_id = ?", (default_dir_id, user_id))
    conn.commit()
    conn.close()

if not os.path.exists(DATABASE):
    init_db()

migrate_db()


# split the text fot large files
def split_text(text, max_size):
    chunks = []
    for i in range(0, len(text), max_size):
        chunks.append(text[i:i+max_size])
    print(f"Totle length：{len(text)}，split to {len(chunks)} chunks")
    return chunks



# Flask routing
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        hashed_pw = generate_password_hash(password, method='pbkdf2:sha256')
        conn = get_db_connection()
        try:
            conn.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, hashed_pw))
            conn.commit()
            flash("Registration successful, please log in.", "success")
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash("Username already exists.", "danger")
        finally:
            conn.close()
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        conn = get_db_connection()
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        conn.close()
        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            print("Login successful, User ID:", user['id'])
            return redirect(url_for('dashboard'))
        else:
            flash("Login failed, please chech username or password.", "danger")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
        
    project_id = request.args.get('project_id', type=int)
    conn = get_db_connection()
    directories = conn.execute("SELECT * FROM directories WHERE user_id = ? AND parent_id IS NULL", (session['user_id'],)).fetchall()
    
    if not directories:
        conn.execute("INSERT INTO directories (user_id, name) VALUES (?, ?)", (session['user_id'], '預設專案'))
        conn.commit()
        directories = conn.execute("SELECT * FROM directories WHERE user_id = ? AND parent_id IS NULL", (session['user_id'],)).fetchall()
        
    if not project_id and directories:
        project_id = directories[0]['id']
        
    current_project = next((d for d in directories if d['id'] == project_id), None)
    files = conn.execute("SELECT * FROM files WHERE user_id = ? AND directory_id = ?", (session['user_id'], project_id)).fetchall()
    conn.close()
    
    return render_template('dashboard.html', directories=directories, files=files, current_project=current_project)

@app.route('/create_project', methods=['POST'])
def create_project():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    project_name = request.form.get('project_name', '').strip()
    if not project_name:
        flash("請輸入專案名稱", "danger")
        return redirect(url_for('dashboard'))
        
    conn = get_db_connection()
    conn.execute("INSERT INTO directories (user_id, name) VALUES (?, ?)", (session['user_id'], project_name))
    project_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    
    flash(f"專案 '{project_name}' 建立成功", "success")
    return redirect(url_for('dashboard', project_id=project_id))
    
@app.route('/delete_project/<int:project_id>', methods=['POST'])
def delete_project(project_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    conn = get_db_connection()
    proj = conn.execute("SELECT * FROM directories WHERE id = ? AND user_id = ?", (project_id, session['user_id'])).fetchone()
    if proj:
        # Get files to physically delete
        files = conn.execute("SELECT * FROM files WHERE directory_id = ? AND user_id = ?", (project_id, session['user_id'])).fetchall()
        for f in files:
            try:
                if os.path.exists(f['filepath']):
                    os.remove(f['filepath'])
            except:
                pass
        conn.execute("DELETE FROM files WHERE directory_id = ? AND user_id = ?", (project_id, session['user_id']))
        conn.execute("DELETE FROM directories WHERE id = ? AND user_id = ?", (project_id, session['user_id']))
        conn.commit()
        flash("專案已徹底刪除", "success")
    conn.close()
    return redirect(url_for('dashboard'))

@app.route('/rename_project/<int:project_id>', methods=['POST'])
def rename_project(project_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
        
    new_name = request.form.get('new_name', '').strip()
    if not new_name:
        flash("請輸入有效的新專案名稱", "danger")
        return redirect(url_for('dashboard', project_id=project_id))
        
    conn = get_db_connection()
    proj = conn.execute("SELECT * FROM directories WHERE id = ? AND user_id = ?", (project_id, session['user_id'])).fetchone()
    if proj:
        conn.execute("UPDATE directories SET name = ? WHERE id = ? AND user_id = ?", (new_name, project_id, session['user_id']))
        conn.commit()
        flash("專案名稱已更新", "success")
    else:
        flash("無權限或專案不存在", "danger")
    conn.close()
    
    return redirect(url_for('dashboard', project_id=project_id))

@app.route('/about')
def about():
    return render_template('about.html')

@app.route('/upload_file', methods=['POST'])
def upload_file():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    if 'file' not in request.files:
        flash("No file uploaded.", "danger")
        return redirect(url_for('dashboard'))
    files = request.files.getlist('file')
    directory_id = request.form.get('directory_id')
    if not directory_id:
        flash("無法判斷專案歸屬，上傳失敗", "danger")
        return redirect(url_for('dashboard'))
    
    conn = get_db_connection()
    success_count = 0
    fail_count = 0
    for file in files:
        if file and allowed_file(file.filename):
            original_filename = file.filename
            filename = f"{uuid.uuid4()}_{secure_filename(original_filename)}"
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            conn.execute("INSERT INTO files (directory_id, user_id, filename, filepath) VALUES (?, ?, ?, ?)",
                         (directory_id, session['user_id'], original_filename, filepath))
            success_count += 1
        else:
            if file.filename:
                fail_count += 1
                
    conn.commit()
    conn.close()
    
    if success_count > 0:
        flash(f"{success_count} files uploaded successfully.", "success")
    if fail_count > 0:
        flash(f"{fail_count} files failed (Type mismatch or empty). only support .txt, .pdf, and .docx!", "danger")
        
    return redirect(url_for('dashboard', project_id=directory_id))

@app.route('/api/preview/<int:file_id>')
def api_preview(file_id):
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 403
    conn = get_db_connection()
    file = conn.execute("SELECT * FROM files WHERE id = ? AND user_id = ?", (file_id, session['user_id'])).fetchone()
    conn.close()
    
    if file:
        filename = file['filename'].lower()
        if filename.endswith('.pdf'):
            physical_filename = os.path.basename(file['filepath'])
            file_url = url_for('uploaded_file', filename=physical_filename)
            return jsonify({"type": "pdf", "url": file_url})
        elif filename.endswith('.docx'):
            doc_html = extract_html_from_docx(file['filepath'])
            return jsonify({"type": "html", "content": doc_html})
        else:
            try:
                with open(file['filepath'], 'r', encoding='utf-8') as f:
                    content = f.read()
            except Exception as e:
                content = "Unable to preview this file type."
            return jsonify({"type": "text", "content": content})
    else:
        return jsonify({"error": "File not found"}), 404

@app.route('/preview/<int:file_id>')
def preview(file_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    conn = get_db_connection()
    file = conn.execute("SELECT * FROM files WHERE id = ? AND user_id = ?", (file_id, session['user_id'])).fetchone()
    conn.close()
    if file:
        filename = file['filename'].lower()
        if filename.endswith('.pdf'):
            physical_filename = os.path.basename(file['filepath'])
            file_url = url_for('uploaded_file', filename=physical_filename)
            return render_template('preview.html', file=file, is_pdf=True, file_url=file_url)
        else:
            try:
                with open(file['filepath'], 'r', encoding='utf-8') as f:
                    content = f.read()
            except Exception as e:
                content = "Unable to preview this file type."
            return render_template('preview.html', file=file, is_pdf=False, content=content)
    else:
        flash("File no exist", "danger")
        return redirect(url_for('dashboard'))

@app.route('/delete_file/<int:file_id>', methods=['POST'])
def delete_file(file_id):
    if 'user_id' not in session:
        flash("Please login firstly.", "danger")
        return redirect(url_for('login'))
    conn = get_db_connection()
    file = conn.execute("SELECT * FROM files WHERE id = ? AND user_id = ?", (file_id, session['user_id'])).fetchone()
    if file is None:
        conn.close()
        flash("File does not exist or you do not have permission to delete it.", "danger")
        return redirect(url_for('dashboard'))
    conn.execute("DELETE FROM files WHERE id = ? AND user_id = ?", (file_id, session['user_id']))
    conn.commit()
    conn.close()
    try:
        if os.path.exists(file['filepath']):
            os.remove(file['filepath'])
    except Exception as e:
        flash("File data has been deleted, but the physical file deletion failed.", "warning")
        return redirect(url_for('dashboard', project_id=file['directory_id']))
    flash("File data has been deleted", "success")
    return redirect(url_for('dashboard', project_id=file['directory_id']))

@app.route('/generate_test', methods=['POST'])
def generate_test():
    if 'user_id' not in session:
        return jsonify({"error": "Not logged in."}), 403
    try:
        num_single = int(request.form.get('num_single', 0))
        num_multiple = int(request.form.get('num_multiple', 0))
        num_boolean = int(request.form.get('num_boolean', 0))
    except ValueError:
        num_single = num_multiple = num_boolean = 1

    file_ids = request.form.getlist('file_ids[]')
    if not file_ids:
        return jsonify({"error": "No file selected."}), 400

    content_text = ""
    conn = get_db_connection()
    for fid in file_ids:
        file = conn.execute("SELECT * FROM files WHERE id = ? AND user_id = ?", (fid, session['user_id'])).fetchone()
        if file:
            filename = file['filename'].lower()
            if filename.endswith('.pdf'):
                pdf_text = extract_text_from_pdf(file['filepath'])
                print(f"PDF file {file['filename']} has {len(pdf_text)} words")
                content_text += pdf_text + "\n"
            elif filename.endswith('.docx'):
                doc_html = extract_html_from_docx(file['filepath'])
                print(f"DOCX file {file['filename']} HTML length: {len(doc_html)}")
                content_text += doc_html + "\n"
            else:
                try:
                    with open(file['filepath'], 'r', encoding='utf-8') as f:
                        file_text = f.read()
                        print(f"File {file['filename']} Length:{len(file_text)}")
                        content_text += file_text + "\n"
                except Exception as e:
                    print(f"File {file['filepath']} reading error:{e}")
                    continue
    conn.close()

    if not content_text.strip():
        return jsonify({"error": "File content cannot be read or is empty."}), 400

    CHUNK_SIZE = 120000
    if len(content_text) > CHUNK_SIZE:
        print("Content is long, splitting into chunks for parallel test generation...")
        chunks = split_text(content_text, CHUNK_SIZE)
    else:
        chunks = [content_text]
        print("Content length is within limits, using the original text directly.")
    
    num_chunks = len(chunks)
    
    def distribute(total, n):
        b = total // n
        r = total % n
        return [b + 1 if i < r else b for i in range(n)]
        
    singles = distribute(num_single, num_chunks)
    multiples = distribute(num_multiple, num_chunks)
    booleans = distribute(num_boolean, num_chunks)
    
    import json
    all_questions = []
    error_details = []

    for i, chunk in enumerate(chunks):
        s, m, b = singles[i], multiples[i], booleans[i]
        if s == 0 and m == 0 and b == 0:
            continue
            
        print(f"Processing chunk {i+1}/{num_chunks}: requesting {s} single, {m} multiple, {b} boolean questions.")
        prompt = (
            f"根據以下內容，請生成自我測驗題目：\n"
            f"請生成 {s} 個單選題、{m} 個多選題，以及 {b} 個是非題。\n"
            f"請只給出題目、選項（如適用）、正確答案與解釋，並以 JSON 格式回應。\n\n"
            f"請僅回傳 JSON 格式的回應，不要包含任何其他文字、說明或標籤。\n"
            f"請回傳的內容必須是一個合法的 JSON 字串，且嚴格符合以下格式：\n\n"
            '{ "questions": [\n'
            '  {\n'
            '    "type": "single",\n'
            '    "question": "問題內容",\n'
            '    "options": ["選項A", "選項B", "選項C", "選項D"],\n'
            '    "answer": "A",\n'
            '    "explanation": "解釋內容"\n'
            '  },\n'
            '  {\n'
            '    "type": "multiple",\n'
            '    "question": "問題內容",\n'
            '    "options": ["選項A", "選項B", "選項C", "選項D"],\n'
            '    "answer": ["A", "C"],\n'
            '    "explanation": "解釋內容"\n'
            '  },\n'
            '  {\n'
            '    "type": "boolean",\n'
            '    "question": "問題內容",\n'
            '    "answer": "True",\n'
            '    "explanation": "解釋內容"\n'
            '  }\n'
            ']}\n\n'
            f"以下是參考內容：\n{chunk}\n"
        )
        try:
            response = client.messages.create(
                model="claude-3-haiku-20240307",
                max_tokens=4000,
                temperature=0.7,
                system="你是一個出題專家，請依據使用者提供的內容產生精確且高品質的自我測驗題目。",
                messages=[
                    {"role": "user", "content": prompt}
                ]
            )
            result_text = response.content[0].text
            start_idx = result_text.find('{')
            end_idx = result_text.rfind('}')
            if start_idx != -1 and end_idx != -1:
                json_str = result_text[start_idx:end_idx+1]
                try:
                    parsed = json.loads(json_str)
                    if "questions" in parsed:
                        all_questions.extend(parsed["questions"])
                except json.JSONDecodeError as e:
                    error_details.append(f"JSON Parse Error: {e}")
                    print(f"JSON fail on chunk {i+1}: {e}")
            else:
                error_details.append(f"No JSON boundary found in response.")
                print(f"Failed to find JSON in chunk {i+1} response.")
                
        except Exception as e:
            error_details.append(f"API Error: {str(e)}")
            print(f"Anthropic API Error on chunk {i+1}:", e)
            continue # 不要因為一個小區塊失敗就整副牌組壞掉

    if not all_questions:
        error_msg = "Failed to generate valid test questions."
        if error_details:
             error_msg += f" Details: {error_details[0]}"
        return jsonify({"error": error_msg}), 500

    print(f"Total quiz questions combined successfully: {len(all_questions)}")
    return jsonify({"test": {"questions": all_questions}})


@app.route('/workbook')
@app.route('/workbook/<int:project_id>')
def workbook(project_id=None):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    
    directories = conn.execute("SELECT * FROM directories WHERE user_id = ? AND parent_id IS NULL", (session['user_id'],)).fetchall()
    
    if project_id is None:
        conn.close()
        # 列出所有專案，讓使用者選擇要進入哪一個練習本
        return render_template('workbook.html', directories=directories, current_project=None, questions=None)
        
    # 確保 project_id 屬於該使用者
    current_project = conn.execute("SELECT * FROM directories WHERE id = ? AND user_id = ?", (project_id, session['user_id'])).fetchone()
    if not current_project:
        conn.close()
        flash("專案不存在或無權限存取", "danger")
        return redirect(url_for('workbook'))
        
    questions = conn.execute("SELECT * FROM saved_questions WHERE user_id = ? AND directory_id = ? ORDER BY saved_at DESC", (session['user_id'], project_id)).fetchall()
    conn.close()
    
    # 解析 JSON 字串轉回 Python 物件，以便在前端模板渲染
    parsed_questions = []
    for q in questions:
        q_dict = dict(q)
        q_dict['options'] = json.loads(q_dict['options']) if q_dict['options'] else []
        q_dict['answer'] = json.loads(q_dict['answer']) if q_dict['type'] == 'multiple' else q_dict['answer']
        parsed_questions.append(q_dict)
        
    return render_template('workbook.html', directories=directories, current_project=current_project, questions=parsed_questions)


@app.route('/api/save_question', methods=['POST'])
def save_question():
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 403
    
    data = request.json
    if not data or 'question' not in data:
        return jsonify({"error": "Invalid data"}), 400
        
    q_type = data.get('type', 'single')
    question = data.get('question', '')
    options_json = json.dumps(data.get('options', []), ensure_ascii=False)
    directory_id = data.get('directory_id')
    
    answer = data.get('answer', '')
    # 如果是多選題，answer 可能是陣列，需要轉成 JSON 字串
    if isinstance(answer, list):
        answer_to_save = json.dumps(answer, ensure_ascii=False)
    else:
        answer_to_save = answer
        
    explanation = data.get('explanation', '')
    
    conn = get_db_connection()
    conn.execute('''
        INSERT INTO saved_questions (user_id, directory_id, type, question, options, answer, explanation)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (session['user_id'], directory_id, q_type, question, options_json, answer_to_save, explanation))
    conn.commit()
    conn.close()
    
    return jsonify({"success": True})


@app.route('/api/delete_question/<int:question_id>', methods=['DELETE'])
def delete_question(question_id):
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 403
        
    conn = get_db_connection()
    # 確保只能刪除自己的題目
    conn.execute("DELETE FROM saved_questions WHERE id = ? AND user_id = ?", (question_id, session['user_id']))
    conn.commit()
    conn.close()
    
    return jsonify({"success": True})


@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5001)
