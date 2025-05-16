import json
import sqlite3


def evaluate():
    db = sqlite3.connect(':memory:')
    cursor = db.cursor()
    output = {}
    try:
        # Read schema
        with open('/app/schema.sql', 'r') as f:
            schema = f.read()
        if schema.strip():
            cursor.executescript(schema)

        # Read and execute user query
        with open('/app/user_query.sql', 'r') as f:
            user_query = f.read()
        cursor.execute(user_query)
        user_results = cursor.fetchall()
        user_cols = [desc[0] for desc in cursor.description] if cursor.description else []

        # Read and execute expected query
        with open('/app/expected_query.sql', 'r') as f:
            expected_query = f.read()
        cursor.execute(expected_query)
        expected_results = cursor.fetchall()
        expected_cols = [desc[0] for desc in cursor.description] if cursor.description else []

        # Format output
        output['user_cols'] = user_cols
        output['user_results'] = user_results
        output['expected_cols'] = expected_cols
        output['expected_results'] = expected_results
        output['error'] = None
    except sqlite3.Error as e:
        output['error'] = str(e)
    except Exception as e:
        output['error'] = f"Unexpected error: {str(e)}"
    finally:
        db.close()
    print(json.dumps(output))


if __name__ == '__main__':
    evaluate()
