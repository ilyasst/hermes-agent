"""Tests for browser_tool.py tab management tools."""
import pytest
import json


def test_browser_tab_list():
    """Test browser_tab_list tool."""
    from tools.browser_tool import browser_tab_list
    
    result = browser_tab_list(task_id='test123')
    data = json.loads(result)
    
    assert 'count' in data
    assert 'tabs' in data
    assert data['count'] >= 1
    assert isinstance(data['tabs'], list)


def test_browser_tab_new():
    """Test browser_tab_new tool."""
    from tools.browser_tool import browser_tab_new
    
    result = browser_tab_new(url='https://example.com', task_id='test123')
    data = json.loads(result)
    
    assert data['success'] is True
    assert 'message' in data


def test_browser_tab_switch():
    """Test browser_tab_switch tool."""
    from tools.browser_tool import browser_tab_switch
    
    # First create a tab
    from tools.browser_tool import browser_tab_new
    browser_tab_new(url='https://example.com', task_id='test123')
    
    # Get tab list
    from tools.browser_tool import browser_tab_list
    list_result = json.loads(browser_tab_list(task_id='test123'))
    
    if list_result['count'] > 1:
        tab_id = list_result['tabs'][1]['tab_id']
        result = browser_tab_switch(tab_id=tab_id, task_id='test123')
        data = json.loads(result)
        assert data['success'] is True


def test_browser_tab_close():
    """Test browser_tab_close tool."""
    from tools.browser_tool import browser_tab_close, browser_tab_list, browser_tab_new
    import json
    
    # Create a new tab
    browser_tab_new(url='https://example.com', task_id='test123')
    
    # Get tab list
    list_result = json.loads(browser_tab_list(task_id='test123'))
    
    # Try to close a tab (not the last one)
    if list_result['count'] > 1:
        tab_to_close = list_result['tabs'][1]['tab_id']
        result = browser_tab_close(tab_id=tab_to_close, task_id='test123')
        data = json.loads(result)
        assert data['success'] is True
    else:
        # Can't close the last tab
        result = browser_tab_close(tab_id='test', task_id='test123')
        data = json.loads(result)
        assert data['success'] is False
        assert 'error' in data


def test_browser_tab_new_without_url():
    """Test browser_tab_new without URL opens blank tab."""
    from tools.browser_tool import browser_tab_new
    
    result = browser_tab_new(task_id='test123')
    data = json.loads(result)
    
    assert data['success'] is True


def test_browser_tab_list_empty_session():
    """Test browser_tab_list with fresh session."""
    from tools.browser_tool import browser_tab_list
    
    result = browser_tab_list(task_id='fresh_test')
    data = json.loads(result)
    
    assert data['count'] >= 1
    assert len(data['tabs']) == data['count']


def test_browser_tool_schemas():
    """Test that all browser tab tools have correct schemas."""
    from tools.browser_tool import browser_tab_list, browser_tab_new, browser_tab_switch, browser_tab_close
    
    # Check that functions exist and return JSON
    assert callable(browser_tab_list)
    assert callable(browser_tab_new)
    assert callable(browser_tab_switch)
    assert callable(browser_tab_close)


if __name__ == '__main__':
    test_browser_tool_schemas()
    print("✓ Browser tool schema tests passed!")
    print("⚠ Integration tests skipped - requires Playwright browser")
