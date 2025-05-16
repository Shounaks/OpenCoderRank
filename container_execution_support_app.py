import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

import docker
import flask
from flask import Flask, render_template, request, redirect, url_for, session, jsonify

import questions_data

app = Flask(__name__)
app.secret_key = os.urandom(24)

# --- Constants ---
DATABASE = 'scoreboard.db'
SCHEMA_FILE = 'schema.sql'

CHALLENGES = {
    "sql_basics": {
        "id": "sql_basics",
        "name": "SQL Basics",
        "description": "A collection of fundamental SQL questions.",
    },
    "python_basic_problems": {
        "id": "python_basic_problems",
        "name": "Python Basic Problems",
        "description": "A collection of python basic and theory questions.",
    }
}

# --- Container Configuration ---
CONTAINER_CONFIG = {
    "python": {
        "image": "python:3.9-slim",
        "extension": ".py",
        "command": ["python", "/app/eval_python.py"]
    },
    "sql": {
        "image": "python:3.9-slim",
        "extension": ".py",
        "command": ["python", "/app/eval_sql.py"]
    }
}


def setup_container_working_directory() -> str:
    work_dir = f"/tmp/exec_{uuid.uuid4()}"
    os.makedirs(work_dir, exist_ok=True)
    return work_dir


def cleanup_container_working_directory(work_dir: str):
    if os.path.exists(work_dir):
        shutil.rmtree(work_dir)


# --- Database Helper Functions ---
def get_db():
    db = getattr(flask.g, '_database', None)
    if db is None:
        db = flask.g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(flask.g, '_database', None)
    if db is not None:
        db.close()


def init_db():
    with app.app_context():
        db = get_db()
        schema_path = os.path.join(app.root_path, SCHEMA_FILE)
        if not os.path.exists(schema_path):
            print(f"ERROR: {SCHEMA_FILE} not found at {schema_path}. Database cannot be initialized properly via CLI.",
                  file=sys.stderr)
            return
        with app.open_resource(SCHEMA_FILE, mode='r') as f:
            db.cursor().executescript(f.read())
        db.commit()
        print("Initialized the database.")


def query_db(query, args=(), one=False):
    cur = get_db().execute(query, args)
    rv = cur.fetchall()
    cur.close()
    return (rv[0] if rv else None) if one else rv


def execute_db(query, args=()):
    db = get_db()
    cur = db.cursor()
    cur.execute(query, args)
    db.commit()
    cur.close()


def _get_qnp_data(session_question_ids, session_answers):
    qnp_data = []
    for q_id_in_list in session_question_ids:
        status = session_answers.get(str(q_id_in_list), {}).get('status', 'unattempted')
        qnp_data.append({'id': q_id_in_list, 'status': status})
    return qnp_data


# --- Routes ---
@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        username = request.form.get('username')
        challenge_id = request.form.get('challenge_id')
        if not username or len(username.strip()) < 2:
            flask.flash("Please enter a valid name (at least 2 characters).", "error")
            return render_template('index.html', challenges=CHALLENGES)
        if not challenge_id or challenge_id not in CHALLENGES:
            flask.flash("Please select a valid challenge.", "error")
            return render_template('index.html', challenges=CHALLENGES)
        session.clear()
        session['username'] = username.strip()
        session['challenge_id'] = challenge_id
        session['current_question_idx'] = 0
        session['score'] = 0
        session['start_time'] = time.time()
        session['question_start_time'] = time.time()
        challenge_questions_metadata = questions_data.get_all_questions_metadata(challenge_id)
        session['question_ids'] = [q['id'] for q in challenge_questions_metadata]
        if not session['question_ids']:
            flask.flash(
                f"No questions found for challenge '{CHALLENGES[challenge_id]['name']}'. Please select another.",
                "warning")
            session.clear()
            return render_template('index.html', challenges=CHALLENGES)
        session['answers'] = {str(qid): {"status": "unattempted", "attempt_detail": None} for qid in
                              session['question_ids']}
        return redirect(url_for('test_page'))
    return render_template('index.html', challenges=CHALLENGES)


@app.route('/test')
def test_page():
    if 'username' not in session or 'challenge_id' not in session:
        flask.flash("Please start a new test.", "warning")
        return redirect(url_for('index'))
    challenge_id = session['challenge_id']
    challenge_name = CHALLENGES.get(challenge_id, {}).get('name', "Unknown Challenge")
    num_questions = len(session.get('question_ids', []))
    if num_questions == 0:
        flask.flash("No questions available for this challenge. Please restart.", "error")
        return redirect(url_for('index'))
    return render_template('test.html',
                           username=session['username'],
                           num_questions=num_questions,
                           challenge_name=challenge_name)


@app.route('/api/question', methods=['GET'])
def get_current_question_api():
    if 'username' not in session or 'challenge_id' not in session:
        return jsonify({"error": "Not authenticated or challenge not selected"}), 401
    current_idx = session.get('current_question_idx', 0)
    question_ids = session.get('question_ids', [])
    challenge_id = session['challenge_id']
    qnp_data = _get_qnp_data(session.get('question_ids', []), session.get('answers', {}))
    if not question_ids or (current_idx >= len(question_ids) > 0):
        return jsonify({
            "test_completed": True,
            "score": session.get('score', 0),
            "qnp_data": qnp_data,
            "message": "No questions in this challenge." if not question_ids else "Test completed."
        })
    q_id = question_ids[current_idx]
    question = questions_data.get_question_by_id(q_id)
    if not question or question.get('challenge_id') != challenge_id:
        return jsonify({"error": "Question not found or not part of this challenge"}), 404
    session['question_start_time'] = time.time()
    client_question = {
        'id': question['id'],
        'title': question['title'],
        'level': question['level'],
        'language': question['language'],
        'description': question['description'],
        'points': question['points'],
        'time_limit_seconds': question['time_limit_seconds'],
        'remarks': question.get('remarks')
    }
    if question['language'] == 'python':
        client_question['starter_code'] = question.get('starter_code', '')
    elif question['language'] == 'sql':
        client_question['schema'] = question.get('schema', '')
        client_question['starter_query'] = question.get('starter_query', '')
    elif question['language'] == 'mcq':
        client_question['options'] = question.get('options', [])
    client_question['current_q_num'] = current_idx + 1
    client_question['total_questions'] = len(question_ids)
    client_question['user_score'] = session.get('score', 0)
    client_question['challenge_name'] = CHALLENGES.get(challenge_id, {}).get('name', "Unknown Challenge")
    client_question['qnp_data'] = qnp_data
    return jsonify(client_question)


@app.route('/api/evaluate', methods=['POST'])
def evaluate_code_api():
    if 'username' not in session or 'challenge_id' not in session:
        return jsonify({"error": "Not authenticated or challenge not selected"}), 401
    data = request.get_json()
    user_submission = data.get('code')
    q_id = data.get('question_id')
    challenge_id = session['challenge_id']
    q_id_str = str(q_id)
    if user_submission is None or q_id is None:
        return jsonify({"error": "Missing code/answer or question_id"}), 400
    question = questions_data.get_question_by_id(int(q_id))
    if not question or question.get('challenge_id') != challenge_id:
        return jsonify({"error": "Invalid question_id or not part of this challenge"}), 400
    current_answer_info = session['answers'].get(q_id_str, {})
    if current_answer_info.get('status') == 'correct':
        updated_qnp_data = _get_qnp_data(session.get('question_ids', []), session.get('answers', {}))
        return jsonify({
            "status": "already_correct",
            "message": "You have already answered this question correctly.",
            "output": current_answer_info.get('attempt_detail', ''),
            "qnp_data": updated_qnp_data,
            "new_score": session.get('score')
        })
    result = {"status": "error", "output": "Evaluation failed.", "passed_all_tests": False}
    if question['language'] == 'sql':
        result = evaluate_sql_containerized(user_submission, question)
    elif question['language'] == 'python':
        result = evaluate_python_containerized(user_submission, question)
    elif question['language'] == 'mcq':
        result = evaluate_mcq(user_submission, question)
    if result.get('passed_all_tests'):
        if current_answer_info.get('status') != 'correct':
            session['score'] = session.get('score', 0) + question['points']
        session['answers'][q_id_str] = {"status": "correct", "attempt_detail": result.get("output", "")}
        result['new_score'] = session['score']
    else:
        if current_answer_info.get('status') != 'correct':
            session['answers'][q_id_str] = {"status": "incorrect", "attempt_detail": result.get("output", "")}
        else:
            result['message'] = "Evaluated, but score retained from first correct answer."
    result['qnp_data'] = _get_qnp_data(session.get('question_ids', []), session.get('answers', {}))
    return jsonify(result)


@app.route('/api/evaluate_containerized', methods=['POST'])
def evaluate_code_containerized_api():
    if 'username' not in session or 'challenge_id' not in session:
        return jsonify({"error": "Not authenticated or challenge not selected"}), 401
    data = request.get_json()
    user_submission = data.get('code')
    q_id = data.get('question_id')
    challenge_id = session['challenge_id']
    q_id_str = str(q_id)
    if user_submission is None or q_id is None:
        return jsonify({"error": "Missing code/answer or question_id"}), 400
    question = questions_data.get_question_by_id(int(q_id))
    if not question or question.get('challenge_id') != challenge_id:
        return jsonify({"error": "Invalid question_id or not part of this challenge"}), 400
    current_answer_info = session['answers'].get(q_id_str, {})
    if current_answer_info.get('status') == 'correct':
        updated_qnp_data = _get_qnp_data(session.get('question_ids', []), session.get('answers', {}))
        return jsonify({
            "status": "already_correct",
            "message": "You have already answered this question correctly.",
            "output": current_answer_info.get('attempt_detail', ''),
            "qnp_data": updated_qnp_data,
            "new_score": session.get('score')
        })
    result = {"status": "error", "output": "Evaluation failed.", "passed_all_tests": False}
    if question['language'] == 'sql':
        result = evaluate_sql_containerized(user_submission, question)
    elif question['language'] == 'python':
        result = evaluate_python_containerized(user_submission, question)
    elif question['language'] == 'mcq':
        result = evaluate_mcq(user_submission, question)
    if result.get('passed_all_tests'):
        if current_answer_info.get('status') != 'correct':
            session['score'] = session.get('score', 0) + question['points']
        session['answers'][q_id_str] = {"status": "correct", "attempt_detail": result.get("output", "")}
        result['new_score'] = session['score']
    else:
        if current_answer_info.get('status') != 'correct':
            session['answers'][q_id_str] = {"status": "incorrect", "attempt_detail": result.get("output", "")}
        else:
            result['message'] = "Evaluated, but score retained from first correct answer."
    result['qnp_data'] = _get_qnp_data(session.get('question_ids', []), session.get('answers', {}))
    return jsonify(result)


def evaluate_mcq(selected_option_index_str, question_data):
    try:
        selected_index = int(selected_option_index_str)
    except ValueError:
        return {
            "status": "error",
            "output": "<p class='text-danger'>Invalid answer format.</p>",
            "passed_all_tests": False
        }
    is_correct = (selected_index == question_data['correct_answer_index'])
    output_html = ""
    if is_correct:
        output_html = f"<p class='text-success mt-2'><strong>Status: Correct!</strong></p>"
    else:
        correct_option_text = "N/A"
        if 0 <= question_data['correct_answer_index'] < len(question_data['options']):
            correct_option_text = question_data['options'][question_data['correct_answer_index']]
        output_html = f"<p class='text-danger mt-2'><strong>Status: Incorrect.</strong></p>"
        output_html += f"<p>The correct answer was: '{flask.escape(correct_option_text)}'</p>"
    return {
        "status": "correct" if is_correct else "incorrect",
        "output": output_html,
        "passed_all_tests": is_correct
    }


def evaluate_sql(user_query, question_data):
    db_eval = sqlite3.connect(':memory:')
    cursor_eval = db_eval.cursor()
    output_html = ""
    is_correct = False
    error_message = None
    try:
        if question_data.get('schema'):
            cursor_eval.executescript(question_data['schema'])
        cursor_eval.execute(user_query)
        user_results_raw = cursor_eval.fetchall()
        user_cols = [desc[0] for desc in cursor_eval.description] if cursor_eval.description else []
        cursor_eval.execute(question_data['expected_query_output'])
        expected_results_raw = cursor_eval.fetchall()
        expected_cols = [desc[0] for desc in cursor_eval.description] if cursor_eval.description else []
        output_html += "<h4>Your Output:</h4>"
        if user_results_raw:
            output_html += "<table class='results-table'><thead><tr>"
            for col in user_cols:
                output_html += f"<th>{col}</th>"
            output_html += "</tr></thead><tbody>"
            for row in user_results_raw:
                output_html += "<tr>"
                for val in row:
                    output_html += f"<td>{val}</td>"
                output_html += "</tr>"
            output_html += "</tbody></table>"
        else:
            output_html += "<p>Your query returned no results.</p>"
        if user_cols == expected_cols and user_results_raw == expected_results_raw:
            is_correct = True
            output_html += "<p class='text-success mt-2'><strong>Status: Correct!</strong></p>"
        else:
            output_html += "<p class='text-danger mt-2'><strong>Status: Incorrect.</strong></p>"
    except sqlite3.Error as e:
        error_message = f"SQL Error: {e}"
        output_html += f"<p class='text-danger'><strong>Error:</strong> {e}</p>"
    finally:
        db_eval.close()
    return {
        "status": "correct" if is_correct else "incorrect",
        "output": output_html,
        "error": error_message,
        "passed_all_tests": is_correct
    }


def evaluate_sql_containerized(user_query, question_data):
    work_dir = setup_container_working_directory()
    print(f"Creating work_dir: {work_dir}")  # Debug
    if not os.access(work_dir, os.W_OK):
        cleanup_container_working_directory(work_dir)
        return {
            "status": "error",
            "output": f"<p class='text-danger'>No write permission for {work_dir}</p>",
            "passed_all_tests": False
        }
    client = docker.from_env()
    try:
        # Copy eval_sql.py from project directory
        project_eval_sql = os.path.join(os.path.dirname(__file__), "eval_sql.py")
        if not os.path.exists(project_eval_sql):
            cleanup_container_working_directory(work_dir)
            return {
                "status": "error",
                "output": f"<p class='text-danger'>eval_sql.py not found in project directory</p>",
                "passed_all_tests": False
            }
        shutil.copy(project_eval_sql, os.path.join(work_dir, "eval_sql.py"))
        # Write user query and schema to files
        with open(os.path.join(work_dir, "user_query.sql"), "w") as f:
            f.write(user_query)
        with open(os.path.join(work_dir, "schema.sql"), "w") as f:
            f.write(question_data.get('schema', ''))
        with open(os.path.join(work_dir, "expected_query.sql"), "w") as f:
            f.write(question_data['expected_query_output'])
        print(f"Files in work_dir: {os.listdir(work_dir)}")  # Debug
        # Run container
        config = CONTAINER_CONFIG['sql']
        container = client.containers.run(
            image=config['image'],
            command=config['command'],
            volumes={os.path.abspath(work_dir): {"bind": "/app", "mode": "rw"}},
            detach=True,
            mem_limit="512m",
            cpu_period=100000,
            cpu_quota=50000,
            network_mode="none"
        )
        result = container.wait(timeout=5)
        output = container.logs(stdout=True, stderr=True).decode("utf-8")
        print(f"Container output: {output}")  # Debug
        output_html = ""
        is_correct = False
        if result['StatusCode'] != 0:
            output_html = f"<p class='text-danger'>Error during SQL execution: {output}</p>"
            return {
                "status": "incorrect",
                "output": output_html,
                "error": output,
                "passed_all_tests": False
            }
        try:
            result_data = json.loads(output)
            user_cols = result_data['user_cols']
            user_results = result_data['user_results']
            expected_cols = result_data['expected_cols']
            expected_results = result_data['expected_results']
            error = result_data['error']
            output_html += "<h4>Your Output:</h4>"
            if user_results:
                output_html += "<table class='results-table'><thead><tr>"
                for col in user_cols:
                    output_html += f"<th>{col}</th>"
                output_html += "</tr></thead><tbody>"
                for row in user_results:
                    output_html += "<tr>"
                    for val in row:
                        output_html += f"<td>{val}</td>"
                    output_html += "</tr>"
                output_html += "</tbody></table>"
            else:
                output_html += "<p>Your query returned no results.</p>"
            if error:
                output_html += f"<p class='text-danger'><strong>Error:</strong> {error}</p>"
            elif user_cols == expected_cols and user_results == expected_results:
                is_correct = True
                output_html += "<p class='text-success mt-2'><strong>Status: Correct!</strong></p>"
            else:
                output_html += "<p class='text-danger mt-2'><strong>Status: Incorrect.</strong></p>"
        except json.JSONDecodeError:
            output_html = f"<p class='text-danger'>Error parsing container output: {output}</p>"
            error = "Invalid output format"
            return {
                "status": "incorrect",
                "output": output_html,
                "error": error,
                "passed_all_tests": False
            }
        return {
            "status": "correct" if is_correct else "incorrect",
            "output": output_html,
            "error": error,
            "passed_all_tests": is_correct
        }
    except docker.errors.ImageNotFound:
        return {
            "status": "error",
            "output": "<p class='text-danger'>Docker image not available</p>",
            "passed_all_tests": False
        }
    except docker.errors.APIError as e:
        return {
            "status": "error",
            "output": f"<p class='text-danger'>Container error: {str(e)}</p>",
            "passed_all_tests": False
        }
    except TimeoutError:
        return {
            "status": "error",
            "output": "<p class='text-danger'>Execution timed out (5 seconds)</p>",
            "passed_all_tests": False
        }
    except OSError as e:
        return {
            "status": "error",
            "output": f"<p class='text-danger'>File operation error: {str(e)}</p>",
            "passed_all_tests": False
        }
    finally:
        cleanup_container_working_directory(work_dir)
        if 'container' in locals():
            container.remove(force=True)


def evaluate_python_containerized(user_code, question_data):
    work_dir = setup_container_working_directory()
    print(f"Creating work_dir: {work_dir}")  # Debug
    if not os.access(work_dir, os.W_OK):
        cleanup_container_working_directory(work_dir)
        return {
            "status": "error",
            "output": f"<p class='text-danger'>No write permission for {work_dir}</p>",
            "passed_all_tests": False
        }
    client = docker.from_env()
    try:
        # Copy eval_python.py from project directory
        project_eval_python = os.path.join(os.path.dirname(__file__), "eval_python.py")
        if not os.path.exists(project_eval_python):
            cleanup_container_working_directory(work_dir)
            return {
                "status": "error",
                "output": f"<p class='text-danger'>eval_python.py not found in project directory</p>",
                "passed_all_tests": False
            }
        shutil.copy(project_eval_python, os.path.join(work_dir, "eval_python.py"))
        # Write user code and test cases to files
        with open(os.path.join(work_dir, "user_code.py"), "w") as f:
            f.write(user_code)
        with open(os.path.join(work_dir, "test_cases.json"), "w") as f:
            json.dump(question_data["test_cases"], f)
        print(f"Files in work_dir: {os.listdir(work_dir)}")  # Debug
        # Run container
        config = CONTAINER_CONFIG['python']
        container = client.containers.run(
            image=config['image'],
            command=config['command'],
            volumes={os.path.abspath(work_dir): {"bind": "/app", "mode": "rw"}},
            detach=True,
            mem_limit="512m",
            cpu_period=100000,
            cpu_quota=50000,
            network_mode="none"
        )
        result = container.wait(timeout=5)
        output = container.logs(stdout=True, stderr=True).decode("utf-8")
        print(f"Container output: {output}")  # Debug
        results_html = ""
        all_tests_passed = True
        if result['StatusCode'] != 0:
            results_html = f"<p class='text-danger'>Error during code execution: {output}</p>"
            return {
                "status": "failed_tests",
                "output": results_html,
                "passed_all_tests": False
            }
        try:
            test_results = json.loads(output)
            results_html += "<ul class='list-group'>"
            for res in test_results:
                status_icon = "✅" if res['passed'] else "❌"
                status_class = "text-success" if res['passed'] else "text-danger"
                results_html += f"<li class='list-group-item'>"
                results_html += f"<strong>{res['name']}:</strong> {status_icon} <span class='{status_class}'>"
                results_html += "Passed" if res['passed'] else "Failed"
                results_html += "</span><br>"
                results_html += f"<small>Input: <code>{res['input']}</code>, Expected: <code>{res['expected']}</code>, Got: <code>{res['actual'] if not res['error'] else 'Error'}</code></small>"
                if res['error']:
                    results_html += f"<br><small class='text-danger'>Error during this test: {res['error']}</small>"
                results_html += "</li>"
                if not res['passed']:
                    all_tests_passed = False
            results_html += "</ul>"
        except json.JSONDecodeError:
            results_html = f"<p class='text-danger'>Error parsing container output: {output}</p>"
            all_tests_passed = False
            return {
                "status": "failed_tests",
                "output": results_html,
                "passed_all_tests": False
            }
        overall_status_message = "<p class='text-success mt-2'><strong>All tests passed!</strong></p>" if all_tests_passed else "<p class='text-danger mt-2'><strong>Some tests failed.</strong></p>"
        return {
            "status": "success" if all_tests_passed else "failed_tests",
            "output": overall_status_message + results_html,
            "passed_all_tests": all_tests_passed
        }
    except docker.errors.ImageNotFound:
        return {
            "status": "error",
            "output": "<p class='text-danger'>Docker image not available</p>",
            "passed_all_tests": False
        }
    except docker.errors.APIError as e:
        return {
            "status": "error",
            "output": f"<p class='text-danger'>Container error: {str(e)}</p>",
            "passed_all_tests": False
        }
    except TimeoutError:
        return {
            "status": "error",
            "output": f"<p class='text-danger'>Execution timed out (5 seconds)</p>",
            "passed_all_tests": False
        }
    except OSError as e:
        return {
            "status": "error",
            "output": f"<p class='text-danger'>File operation error: {str(e)}</p>",
            "passed_all_tests": False
        }
    finally:
        cleanup_container_working_directory(work_dir)
        if 'container' in locals():
            container.remove(force=True)


def evaluate_python(user_code, question_data):
    results_html = ""
    all_tests_passed = True
    overall_status_message = ""
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".py", delete=False) as tmp_code_file:
        tmp_code_file.write("import sys\n")
        tmp_code_file.write("import json\n\n")
        tmp_code_file.write(user_code + "\n\n")
        harness_code = "def run_tests():\n"
        harness_code += "    results = []\n"
        match = re.search(r"def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", user_code)
        if not match:
            func_name = "user_function"
        else:
            func_name = match.group(1)
        for i, test_case in enumerate(question_data["test_cases"]):
            input_args_str = ", ".join(map(repr, test_case["input_args"]))
            harness_code += f"    try:\n"
            harness_code += f"        actual = {func_name}({input_args_str})\n"
            harness_code += f"        passed_check = actual == {repr(test_case['expected_output'])}\n"
            harness_code += f"        results.append({{'name': '{test_case.get('name', f'Test {i + 1}')}', 'input': {test_case['input_args']}, 'expected': {repr(test_case['expected_output'])}, 'actual': actual, 'passed': passed_check, 'error': None}})\n"
            harness_code += f"    except Exception as e_test:\n"
            harness_code += f"        results.append({{'name': '{test_case.get('name', f'Test {i + 1}')}', 'input': {test_case['input_args']}, 'expected': {repr(test_case['expected_output'])}, 'actual': None, 'passed': False, 'error': str(e_test)}})\n"
        harness_code += "    print(json.dumps(results))\n\n"
        harness_code += "run_tests()\n"
        tmp_code_file.write(harness_code)
        tmp_file_name = tmp_code_file.name
    python_executable = sys.executable
    try:
        process = subprocess.run(
            [python_executable, tmp_file_name],
            capture_output=True,
            text=True,
            timeout=5
        )
        if process.returncode == 0:
            try:
                test_results = json.loads(process.stdout)
                results_html += "<ul class='list-group'>"
                for res in test_results:
                    status_icon = "✅" if res['passed'] else "❌"
                    status_class = "text-success" if res['passed'] else "text-danger"
                    results_html += f"<li class='list-group-item'>"
                    results_html += f"<strong>{res['name']}:</strong> {status_icon} <span class='{status_class}'>"
                    results_html += "Passed" if res['passed'] else "Failed"
                    results_html += "</span><br>"
                    results_html += f"<small>Input: <code>{res['input']}</code>, Expected: <code>{res['expected']}</code>, Got: <code>{res['actual'] if not res['error'] else 'Error'}</code></small>"
                    if res['error']:
                        results_html += f"<br><small class='text-danger'>Error during this test: {res['error']}</small>"
                    results_html += "</li>"
                    if not res['passed']:
                        all_tests_passed = False
                results_html += "</ul>"
            except json.JSONDecodeError:
                results_html = "<p class='text-danger'>Error: Could not parse test output from script.</p>"
                results_html += f"<pre>Script STDOUT:\n{process.stdout}</pre>"
                all_tests_passed = False
        else:
            results_html = f"<p class='text-danger'>Error during code execution (Return Code: {process.returncode}):</p>"
            error_output = process.stderr if process.stderr else process.stdout
            results_html += f"<pre>{error_output}</pre>"
            all_tests_passed = False
    except subprocess.TimeoutExpired:
        results_html = "<p class='text-danger'>Error: Code execution timed out (max 5 seconds).</p>"
        all_tests_passed = False
    except Exception as e_outer:
        results_html = f"<p class='text-danger'>An unexpected error occurred during evaluation: {e_outer}</p>"
        all_tests_passed = False
    finally:
        os.remove(tmp_file_name)
    if all_tests_passed:
        overall_status_message = "<p class='text-success mt-2'><strong>All tests passed!</strong></p>"
    else:
        overall_status_message = "<p class='text-danger mt-2'><strong>Some tests failed.</strong></p>"
    return {
        "status": "success" if all_tests_passed else "failed_tests",
        "output": overall_status_message + results_html,
        "passed_all_tests": all_tests_passed
    }


@app.route('/api/jump_to_question', methods=['POST'])
def jump_to_question_api():
    if 'username' not in session or 'challenge_id' not in session:
        return jsonify({"error": "Not authenticated"}), 401
    data = request.get_json()
    target_idx = data.get('index')
    question_ids = session.get('question_ids', [])
    if target_idx is None or not (0 <= target_idx < len(question_ids)):
        return jsonify({"jumped": False, "message": "Invalid question index."}), 400
    session['current_question_idx'] = target_idx
    session['question_start_time'] = time.time()
    return jsonify({"jumped": True, "new_idx": target_idx})


@app.route('/api/previous_question', methods=['POST'])
def previous_question_api():
    if 'username' not in session or 'challenge_id' not in session:
        return jsonify({"error": "Not authenticated or challenge not selected"}), 401
    current_idx = session.get('current_question_idx', 0)
    if current_idx > 0:
        current_idx -= 1
        session['current_question_idx'] = current_idx
        session['question_start_time'] = time.time()
        return jsonify({"navigated": True, "new_idx": current_idx})
    else:
        return jsonify({"navigated": False, "message": "Already at the first question."})


@app.route('/api/next_question', methods=['POST'])
def next_question_api():
    if 'username' not in session or 'challenge_id' not in session:
        return jsonify({"error": "Not authenticated or challenge not selected"}), 401
    current_idx = session.get('current_question_idx', 0)
    question_ids = session.get('question_ids', [])
    challenge_id = session['challenge_id']
    current_idx += 1
    session['current_question_idx'] = current_idx
    if current_idx >= len(question_ids):
        total_time_taken = time.time() - session['start_time']
        execute_db("INSERT INTO scoreboard (username, challenge_id, score, time_taken_seconds) VALUES (?, ?, ?, ?)",
                   (session['username'], challenge_id, session['score'], round(total_time_taken)))
        qnp_data = _get_qnp_data(session.get('question_ids', []), session.get('answers', {}))
        return jsonify({
            "test_completed": True,
            "score": session['score'],
            "total_time": round(total_time_taken),
            "challenge_id": challenge_id,
            "qnp_data": qnp_data
        })
    else:
        session['question_start_time'] = time.time()
        return jsonify({"test_completed": False, "next_question_loaded": True, "new_idx": current_idx})


@app.route('/scoreboards')
def scoreboards_list_page():
    return render_template('scoreboards_list.html', challenges=CHALLENGES)


@app.route('/scoreboard/<challenge_id>')
def scoreboard_page(challenge_id):
    if challenge_id not in CHALLENGES:
        flask.flash("Invalid challenge selected for scoreboard.", "error")
        return redirect(url_for('scoreboards_list_page'))
    challenge = CHALLENGES[challenge_id]
    scores = query_db(
        "SELECT username, score, time_taken_seconds, timestamp FROM scoreboard WHERE challenge_id = ? ORDER BY score DESC, time_taken_seconds ASC LIMIT 20",
        (challenge_id,))
    return render_template('scoreboard.html', scores=scores, challenge=challenge)


@app.route('/restart_test', methods=['POST'])
def restart_test():
    session.clear()
    flask.flash("Test restarted. Please select a challenge and enter your name.", "info")
    return redirect(url_for('index'))


@app.cli.command('initdb')
def initdb_command():
    init_db()


if __name__ == '__main__':
    schema_full_path = os.path.join(os.path.dirname(__file__), SCHEMA_FILE)
    if not os.path.exists(DATABASE):
        print(f"Database {DATABASE} not found. Attempting to initialize...")
        if not os.path.exists(schema_full_path):
            print(
                f"CRITICAL ERROR: {SCHEMA_FILE} not found at {schema_full_path}. Database cannot be created automatically.",
                file=sys.stderr)
            print(
                f"Please create '{SCHEMA_FILE}' or run 'flask initdb' if Flask is installed and '{SCHEMA_FILE}' exists.",
                file=sys.stderr)
            sys.exit(1)
        try:
            db_conn = sqlite3.connect(DATABASE)
            with open(schema_full_path, mode='r') as f:
                db_conn.cursor().executescript(f.read())
            db_conn.commit()
            db_conn.close()
            print(f"Database {DATABASE} created and schema from {SCHEMA_FILE} initialized successfully.")
        except Exception as e:
            print(f"Error initializing database directly: {e}", file=sys.stderr)
            sys.exit(1)
    app.run(debug=True, host='0.0.0.0', port=5555)
