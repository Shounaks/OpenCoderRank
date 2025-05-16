import json
import sys
import re


def run_tests():
    try:
        # Read user code
        with open('/app/user_code.py', 'r') as f:
            user_code = f.read()

        # Read test cases
        with open('/app/test_cases.json', 'r') as f:
            test_cases = json.load(f)

        # Extract function name
        match = re.search(r"def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", user_code)
        if not match:
            print(json.dumps({"error": "No valid function name found"}))
            return
        func_name = match.group(1)

        # Import user code dynamically
        with open('/app/user_code_temp.py', 'w') as f:
            f.write(user_code)
        user_module = __import__('user_code_temp')

        results = []
        for i, test_case in enumerate(test_cases):
            try:
                actual = getattr(user_module, func_name)(*test_case['input_args'])
                passed = actual == test_case['expected_output']
                results.append({
                    'name': test_case.get('name', f'Test {i + 1}'),
                    'input': test_case['input_args'],
                    'expected': test_case['expected_output'],
                    'actual': actual,
                    'passed': passed,
                    'error': None
                })
            except Exception as e:
                results.append({
                    'name': test_case.get('name', f'Test {i + 1}'),
                    'input': test_case['input_args'],
                    'expected': test_case['expected_output'],
                    'actual': None,
                    'passed': False,
                    'error': str(e)
                })
        print(json.dumps(results))
    except Exception as e:
        print(json.dumps({"error": f"Evaluation failed: {str(e)}"}))


if __name__ == '__main__':
    run_tests()
