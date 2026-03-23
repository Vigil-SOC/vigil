# Testing Guide

Complete guide to testing the AI-OpenSOC platform.

## Table of Contents

- [Overview](#overview)
- [Test Infrastructure](#test-infrastructure)
- [Running Tests](#running-tests)
- [Writing Tests](#writing-tests)
- [Coverage Requirements](#coverage-requirements)
- [Best Practices](#best-practices)

---

## Overview

The AI-OpenSOC testing strategy includes:

- **Unit Tests**: Test individual functions and classes in isolation
- **Integration Tests**: Test API endpoints with database and mocked external services
- **End-to-End Tests**: Test complete user workflows (future)
- **Performance Tests**: Benchmark critical operations

### Test Frameworks

**Backend**: pytest with coverage, async support, and mocking
**Frontend**: Vitest with React Testing Library

---

## Test Infrastructure

### Backend Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Test dependencies include:
# - pytest (test framework)
# - pytest-asyncio (async test support)
# - pytest-cov (coverage reporting)
# - pytest-mock (mocking)
# - faker (test data generation)
# - freezegun (time mocking)
# - responses (HTTP mocking)
```

### Frontend Setup

```bash
cd frontend
npm install

# Test dependencies include:
# - vitest (test framework)
# - @testing-library/react (component testing)
# - @testing-library/user-event (user interaction)
# - jsdom (DOM environment)
# - msw (API mocking)
```

---

## Running Tests

### Backend Tests

```bash
# Run all tests
pytest

# Run unit tests only
pytest tests/unit/

# Run integration tests only
pytest tests/integration/

# Run with coverage
pytest --cov=backend --cov=services --cov=daemon --cov-report=html

# Run specific test file
pytest tests/unit/test_auth.py

# Run specific test
pytest tests/unit/test_auth.py::TestPasswordHashing::test_hash_password

# Run tests matching pattern
pytest -k "auth"

# Run with verbose output
pytest -v

# Run in parallel (faster)
pytest -n auto
```

### Frontend Tests

```bash
cd frontend

# Run all tests
npm test

# Run tests once (CI mode)
npm run test:unit

# Watch mode (auto-rerun on changes)
npm run test:watch

# Run with coverage
npm run test:coverage

# Run specific test file
npm test src/utils/api.test.ts
```

### Quick Test Script

```bash
# Run the automated test script
./scripts/run_tests.sh

# Run with verbose output
./scripts/run_tests.sh --verbose
```

---

## Writing Tests

### Unit Test Example

**File**: `tests/unit/test_service.py`

```python
import pytest
from services.my_service import MyService

class TestMyService:
    """Test MyService functionality."""
    
    def test_basic_function(self):
        """Test basic function returns expected result."""
        result = MyService.calculate(10, 5)
        assert result == 15
    
    @pytest.mark.asyncio
    async def test_async_function(self):
        """Test async function."""
        result = await MyService.fetch_data("123")
        assert result is not None
    
    def test_with_mock(self, mocker):
        """Test with mocked dependency."""
        mock_db = mocker.patch('services.my_service.database')
        mock_db.query.return_value = [{"id": "1"}]
        
        result = MyService.get_records()
        assert len(result) == 1
```

### Integration Test Example

**File**: `tests/integration/test_api.py`

```python
import pytest
from fastapi.testclient import TestClient

@pytest.mark.integration
class TestCaseAPI:
    """Integration tests for case API."""
    
    def test_create_case(self, test_client, auth_headers):
        """Test case creation endpoint."""
        response = test_client.post(
            "/api/cases",
            headers=auth_headers,
            json={
                "title": "Test Case",
                "priority": "high",
                "severity": "high"
            }
        )
        
        assert response.status_code == 201
        data = response.json()
        assert data["title"] == "Test Case"
        assert "id" in data
    
    def test_list_cases(self, test_client, auth_headers):
        """Test listing cases."""
        response = test_client.get(
            "/api/cases",
            headers=auth_headers
        )
        
        assert response.status_code == 200
        assert isinstance(response.json(), list)
```

### Frontend Test Example

**File**: `frontend/src/components/CaseList.test.tsx`

```typescript
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import CaseList from './CaseList'

describe('CaseList', () => {
  it('renders case list', () => {
    const cases = [
      { id: '1', title: 'Case 1', priority: 'high' },
      { id: '2', title: 'Case 2', priority: 'medium' }
    ]
    
    render(<CaseList cases={cases} />)
    
    expect(screen.getByText('Case 1')).toBeInTheDocument()
    expect(screen.getByText('Case 2')).toBeInTheDocument()
  })
  
  it('calls onClick when case is clicked', () => {
    const handleClick = vi.fn()
    const cases = [{ id: '1', title: 'Case 1', priority: 'high' }]
    
    render(<CaseList cases={cases} onCaseClick={handleClick} />)
    
    fireEvent.click(screen.getByText('Case 1'))
    expect(handleClick).toHaveBeenCalledWith('1')
  })
})
```

---

## Test Fixtures

### Using Shared Fixtures

Located in `tests/conftest.py`:

```python
def test_with_fixtures(test_db_session, sample_user, auth_headers):
    """Test using multiple fixtures."""
    # test_db_session: Database session
    # sample_user: Pre-created user
    # auth_headers: Authorization headers
    pass
```

### JSON Fixtures

Located in `tests/fixtures/`:

```python
import json

def test_with_json_fixture():
    """Load test data from JSON file."""
    with open('tests/fixtures/sample_findings.json') as f:
        findings = json.load(f)
    
    assert len(findings) > 0
```

---

## Coverage Requirements

### Target Coverage

| Component | Unit Coverage | Integration Coverage |
|-----------|--------------|---------------------|
| Backend API | 85% | 75% |
| Services | 80% | 70% |
| Daemon | 75% | 60% |
| Database | 70% | 80% |
| Frontend | 70% | N/A |

### Viewing Coverage Reports

```bash
# Backend
pytest --cov --cov-report=html
open htmlcov/index.html

# Frontend
npm run test:coverage
open coverage/index.html
```

### Coverage Commands

```bash
# Check if coverage meets thresholds
pytest --cov --cov-fail-under=80

# Generate XML coverage for CI
pytest --cov --cov-report=xml

# Show missing lines
pytest --cov --cov-report=term-missing
```

---

## Test Markers

Use markers to categorize tests:

```python
@pytest.mark.unit
def test_unit():
    pass

@pytest.mark.integration
def test_integration():
    pass

@pytest.mark.slow
def test_slow_operation():
    pass

@pytest.mark.performance
def test_performance_benchmark():
    pass
```

Run tests by marker:

```bash
# Only unit tests
pytest -m unit

# Skip slow tests
pytest -m "not slow"

# Only integration tests
pytest -m integration
```

---

## Mocking External Services

### Mocking Claude API

```python
def test_claude_enrichment(mocker):
    """Test with mocked Claude API."""
    mock_response = {
        "content": [{"type": "text", "text": "Test response"}]
    }
    mocker.patch('services.claude_service.anthropic.Anthropic')
    
    result = ClaudeService.enrich_finding({"id": "test"})
    assert result is not None
```

### Mocking Splunk

```python
@responses.activate
def test_splunk_search():
    """Test with mocked Splunk API."""
    responses.add(
        responses.GET,
        'https://splunk.example.com/services/search',
        json={"results": []},
        status=200
    )
    
    results = SplunkService.search("query")
    assert results == []
```

---

## Best Practices

### 1. Test Naming

```python
# Good
def test_create_case_with_valid_data():
    pass

def test_create_case_with_missing_title_returns_400():
    pass

# Bad
def test1():
    pass

def test_case():
    pass
```

### 2. Arrange-Act-Assert Pattern

```python
def test_calculate_priority():
    # Arrange
    finding = {"severity": "high", "confidence": 0.9}
    
    # Act
    priority = calculate_priority(finding)
    
    # Assert
    assert priority == "high"
```

### 3. One Assertion Per Test (when possible)

```python
# Good
def test_user_has_correct_username():
    user = create_user("testuser")
    assert user.username == "testuser"

def test_user_has_correct_email():
    user = create_user("testuser", "test@example.com")
    assert user.email == "test@example.com"

# Acceptable when testing related properties
def test_user_properties():
    user = create_user("testuser", "test@example.com")
    assert user.username == "testuser"
    assert user.email == "test@example.com"
    assert user.is_active is True
```

### 4. Avoid Test Interdependence

```python
# Bad - tests depend on order
class TestCases:
    case_id = None
    
    def test_create_case(self):
        self.case_id = create_case()
    
    def test_update_case(self):
        update_case(self.case_id)  # Fails if create_case didn't run

# Good - independent tests
class TestCases:
    def test_create_case(self, test_db):
        case_id = create_case()
        assert case_id is not None
    
    def test_update_case(self, test_db, sample_case):
        result = update_case(sample_case.id)
        assert result is True
```

### 5. Use Factories for Test Data

```python
from faker import Faker

fake = Faker()

def create_test_user(**kwargs):
    """Factory for creating test users."""
    defaults = {
        "username": fake.user_name(),
        "email": fake.email(),
        "password": fake.password()
    }
    defaults.update(kwargs)
    return User(**defaults)

# Usage
def test_user_creation():
    user = create_test_user(username="specific_name")
    assert user.username == "specific_name"
```

---

## Debugging Tests

### Running in Debug Mode

```bash
# Run with print statements visible
pytest -s

# Stop on first failure
pytest -x

# Drop into debugger on failure
pytest --pdb

# Run last failed tests
pytest --lf
```

### Using pdb

```python
def test_complex_logic():
    import pdb; pdb.set_trace()
    
    result = complex_function()
    assert result == expected
```

---

## CI/CD Integration

Tests run automatically in GitHub Actions:

- **On Pull Request**: Unit + Integration tests
- **On Push to main**: Full test suite + coverage
- **Nightly**: Full suite + performance tests

See [CI_CD_GUIDE.md](./CI_CD_GUIDE.md) for details.

---

## Troubleshooting

### Common Issues

**Issue**: `ImportError: No module named 'backend'`
**Solution**: Ensure you're running from project root and PYTHONPATH is set

**Issue**: Database tests failing
**Solution**: Ensure PostgreSQL is running: `docker-compose up -d postgres`

**Issue**: Async tests timing out
**Solution**: Increase timeout in pytest.ini or use `@pytest.mark.timeout(30)`

**Issue**: Fixtures not found
**Solution**: Check `conftest.py` is in the right location and fixtures are properly defined

---

## Additional Resources

- [pytest Documentation](https://docs.pytest.org/)
- [Vitest Documentation](https://vitest.dev/)
- [React Testing Library](https://testing-library.com/react)
- [Testing Best Practices](https://testdriven.io/blog/testing-best-practices/)

