# Testing Guide - Vigil-SOC

This guide explains how to run tests, interpret results, and use test feedback with Cursor.

## Quick Start

### Run All Tests Locally
```bash
./scripts/run-tests.sh
```

This script will:
- Run all backend Python tests
- Run all frontend TypeScript tests
- Generate detailed output logs
- Save results to `backend-test-output.log` and `frontend-test-output.log`

### Run Backend Tests Only
```bash
# Run with verbose output
pytest tests/ -v

# Run with detailed error traces
pytest tests/ -v --tb=long

# Run specific test file
pytest tests/unit/test_auth.py -v

# Run tests matching a pattern
pytest tests/ -k "test_login" -v

# Run with coverage
pytest tests/ --cov=backend --cov=services --cov-report=term-missing
```

### Run Frontend Tests Only
```bash
cd frontend

# Run all tests once
npm test -- --run

# Run in watch mode (auto-rerun on changes)
npm run test:watch

# Run with coverage
npm run test:coverage

# Run tests with UI
npm run test:ui

# Run specific test file
npm test -- src/components/__tests__/CasesTable.test.tsx
```

## Understanding Test Output

### Backend Test Output

When tests fail, you'll see output like this:

```
FAILED tests/unit/test_auth.py::TestAuthentication::test_login_invalid_credentials
________________________________ FAILURE ________________________________
    def test_login_invalid_credentials(self):
        """Test login with invalid credentials."""
        result = authenticate_user("user@test.com", "wrong-password")
>       assert result is None
E       AssertionError: assert {'token': '...'} is None

tests/unit/test_auth.py:45: AssertionError
```

**How to read this:**
- `FAILED` - Test that failed
- File path and test name
- Specific assertion that failed
- Line number where it failed

### Frontend Test Output

```
FAIL  src/components/__tests__/CasesTable.test.tsx > CasesTable > handles refresh button click
AssertionError: expected "toHaveBeenCalled" to be called at least once

⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯
 ❯ src/components/__tests__/CasesTable.test.tsx:58:7
```

**How to read this:**
- `FAIL` - Test that failed
- File path and test description
- Assertion error message
- Line number

## Using Test Feedback with Cursor

### From Local Test Runs

1. **Run tests and save output:**
   ```bash
   ./scripts/run-tests.sh
   ```

2. **Open the log files in Cursor:**
   - `backend-test-output.log`
   - `frontend-test-output.log`

3. **Copy failed test output to Cursor chat:**
   - Select the failed test section
   - Paste into Cursor and ask: "Why did this test fail and how do I fix it?"

### From GitHub Actions

1. **When a PR check fails:**
   - Click on the failed check in GitHub
   - Click "Details" to open the Actions run
   - Find the failing step (e.g., "Unit Tests - Backend")
   - Copy the error output

2. **Use with Cursor:**
   - Paste the GitHub Actions output into Cursor chat
   - Cursor can help identify the issue and suggest fixes

3. **Example prompt for Cursor:**
   ```
   This test failed in GitHub Actions:
   
   [paste error output here]
   
   Please help me understand what broke and how to fix it.
   ```

## Test Output Files

After running tests, you'll find:

```
./
├── backend-test-output.log          # Backend test results
├── frontend-test-output.log         # Frontend test results
├── coverage.xml                     # Backend coverage (XML format)
└── frontend/
    └── coverage/                    # Frontend coverage reports
        ├── index.html               # Visual coverage report
        ├── coverage-final.json      # Machine-readable coverage
        └── lcov.info               # LCOV format
```

## Debugging Failed Tests

### Step 1: Identify the Failure
Look for `FAILED` or `FAIL` in the output and note:
- Test name
- File location
- Error message
- Line number

### Step 2: Reproduce Locally
```bash
# Run just the failing test
pytest tests/unit/test_auth.py::TestAuthentication::test_login_invalid_credentials -v

# Or for frontend
npm test -- src/components/__tests__/Login.test.tsx
```

### Step 3: Add Debug Output
```python
# In Python tests
def test_something(self):
    result = my_function()
    print(f"DEBUG: result = {result}")  # This will show in test output
    assert result == expected
```

```typescript
// In TypeScript tests
it('test something', () => {
  const result = myFunction();
  console.log('DEBUG: result =', result);  // Shows in test output
  expect(result).toBe(expected);
});
```

### Step 4: Run with More Verbosity
```bash
# Python - very detailed
pytest tests/ -vv --tb=long --show-capture=all

# Frontend - with console output
npm test -- --reporter=verbose
```

## Common Test Failures

### 1. Component Not Rendering
**Error:** `Unable to find element with text: "Submit"`

**Cause:** Component might not be rendering, or text doesn't match

**Fix:**
```typescript
// Debug what's actually rendered
const { debug } = render(<MyComponent />);
debug(); // Prints entire component tree

// Check if it exists
screen.getByText('Submit'); // Throws if not found
screen.queryByText('Submit'); // Returns null if not found
```

### 2. Async Timing Issues
**Error:** `Timed out waiting for element`

**Fix:**
```typescript
// Wait for element to appear
await waitFor(() => {
  expect(screen.getByText('Loaded')).toBeInTheDocument();
});

// Or use findBy (automatically waits)
const element = await screen.findByText('Loaded');
```

### 3. Mock Not Being Called
**Error:** `expected "toHaveBeenCalled" to be called at least once`

**Fix:**
```typescript
// Verify mock was setup correctly
const mockFn = vi.fn();
render(<MyComponent onSubmit={mockFn} />);

// Check if component is using the prop correctly
await userEvent.click(screen.getByText('Submit'));
console.log('Mock called:', mockFn.mock.calls); // Debug calls
expect(mockFn).toHaveBeenCalled();
```

## Continuous Integration

### GitHub Actions Test Output

When tests run in GitHub Actions:

1. **Viewing Results:**
   - Go to the "Actions" tab in GitHub
   - Click on the workflow run
   - Expand the failing step

2. **Downloading Logs:**
   - Click the ⚙️ gear icon in the top right
   - Select "Download log archive"
   - Extract and open in your editor

3. **Using with Cursor:**
   - Copy the relevant error section
   - Paste into Cursor with context about what you were trying to do

### Test Coverage Reports

Coverage reports show which parts of your code are tested:

```bash
# Generate coverage
npm run test:coverage

# Open HTML report
open frontend/coverage/index.html
```

**Reading Coverage:**
- Green: Well tested (>80%)
- Yellow: Partially tested (50-80%)
- Red: Poorly tested (<50%)

## Writing New Tests

### Test Every Button
```typescript
import { getAllButtons } from '../../test-utils';

it('logs all buttons for debugging', () => {
  const { container } = render(<MyComponent />);
  const buttons = getAllButtons(container);
  
  console.log(`Found ${buttons.length} buttons:`);
  buttons.forEach((btn, idx) => {
    console.log(`  ${idx + 1}. ${btn.textContent}`);
  });
});
```

### Test Every Input
```typescript
import { getAllInputs } from '../../test-utils';

it('logs all inputs for debugging', () => {
  const { container } = render(<MyForm />);
  const inputs = getAllInputs(container);
  
  console.log(`Found ${inputs.length} inputs:`);
  inputs.forEach((input, idx) => {
    console.log(`  ${idx + 1}. ${input.name || input.placeholder}`);
  });
});
```

### Test User Interactions
```typescript
it('tests complete user workflow', async () => {
  const user = userEvent.setup();
  render(<MyComponent />);
  
  // Type into field
  await user.type(screen.getByLabelText('Username'), 'testuser');
  
  // Click button
  await user.click(screen.getByRole('button', { name: 'Submit' }));
  
  // Verify result
  await waitFor(() => {
    expect(screen.getByText('Success')).toBeInTheDocument();
  });
});
```

## Tips for Success

1. **Run tests frequently** - Don't wait until the end
2. **Test one thing at a time** - Isolate failures
3. **Use descriptive test names** - Makes debugging easier
4. **Mock external dependencies** - Tests should be fast and reliable
5. **Check what's actually rendered** - Use `screen.debug()` liberally
6. **Read error messages carefully** - They usually tell you exactly what's wrong

## Getting Help

If tests fail and you're stuck:

1. **Copy the full error output**
2. **Paste into Cursor with context:**
   ```
   I'm testing [component name] and this test is failing:
   
   [paste error]
   
   The component should [describe expected behavior].
   What's wrong?
   ```

3. **Include relevant code:**
   - The test file
   - The component being tested
   - Any related files

## Resources

- [Vitest Documentation](https://vitest.dev/)
- [Testing Library Docs](https://testing-library.com/)
- [Pytest Documentation](https://docs.pytest.org/)
- [Testing Best Practices](https://kentcdodds.com/blog/common-mistakes-with-react-testing-library)

