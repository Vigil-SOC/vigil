const { chromium } = require('playwright');
const fs = require('fs');

(async () => {
  const browser = await chromium.launch({ 
    headless: false,
    args: ['--no-sandbox', '--disable-setuid-sandbox']
  });
  const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 }
  });
  const page = await context.newPage();
  
  // Prevent page from closing unexpectedly
  page.on('close', () => {
    console.log('WARNING: Page was closed!');
  });

  console.log('Step 1: Navigating to http://127.0.0.1:6988...');
  await page.goto('http://127.0.0.1:6988');
  await page.waitForTimeout(3000); // Wait for page to load
  
  console.log('Step 2: Taking screenshot of initial page...');
  await page.screenshot({ path: 'screenshot-1-initial.png', fullPage: true });
  
  console.log('Step 3: Looking for CopilotKit chat interface...');
  
  // Get all visible elements to find chat-related ones
  const pageContent = await page.content();
  fs.writeFileSync('page-html-initial.txt', pageContent);
  console.log('Page HTML saved for analysis');
  
  // Try to find common chat interface selectors - MORE SPECIFIC
  const chatSelectors = [
    'button[class*="copilot" i]',
    'button[class*="chat" i]',
    '[data-copilotkit]',
    '[class*="CopilotKit" i]',
    'button[aria-label*="chat" i]',
    'button[aria-label*="copilot" i]',
    '[data-testid*="chat" i]',
    'button:has-text("Chat")',
    '[role="button"]:has-text("Chat")',
    '.copilotkit-chat-button',
    '#copilot-chat',
    'iframe[title*="copilot" i]',
    'button svg', // Look for buttons with icons
  ];
  
  let chatButton = null;
  let chatInput = null;
  
  console.log('Looking for chat button...');
  
  // Try to find and click chat button
  for (const selector of chatSelectors) {
    try {
      const elements = await page.$$(selector);
      console.log(`Selector ${selector}: found ${elements.length} elements`);
      
      if (elements.length > 0) {
        // Try each element
        for (let i = 0; i < elements.length; i++) {
          const element = elements[i];
          const isVisible = await element.isVisible();
          const text = await element.textContent().catch(() => '');
          const outerHTML = await element.evaluate(el => el.outerHTML.substring(0, 200)).catch(() => '');
          
          console.log(`  Element ${i}: visible=${isVisible}, text="${text}", html="${outerHTML}"`);
          
          // Skip the search input
          if (outerHTML.includes('Search findings')) {
            console.log(`  Skipping search input`);
            continue;
          }
          
          if (isVisible && (outerHTML.toLowerCase().includes('chat') || outerHTML.toLowerCase().includes('copilot') || outerHTML.includes('Vigil'))) {
            console.log(`  Attempting to click element ${i} using force click`);
            chatButton = element;
            
            // Try force click to bypass overlay
            await element.click({ force: true }).catch(async (e) => {
              console.log(`  Force click failed: ${e.message}, trying JavaScript click`);
              await element.evaluate(el => el.click());
            });
            
            await page.waitForTimeout(2000);
            console.log(`  Clicked! Waiting for chat to open...`);
            break;
          }
        }
        
        if (chatButton) break;
      }
    } catch (e) {
      console.log(`  Error with selector ${selector}: ${e.message}`);
    }
  }
  
  console.log('Step 4: Taking screenshot after looking for chat...');
  await page.screenshot({ path: 'screenshot-2-chat-search.png', fullPage: true });
  
  // Wait a bit longer for the chat to fully render
  console.log('Waiting extra time for chat to fully render...');
  await page.waitForTimeout(3000);
  
  // Try to dismiss any announcements
  console.log('Looking for announcement dismiss button...');
  const dismissButton = await page.$('button:has-text("Dismiss")').catch(() => null);
  if (dismissButton) {
    console.log('Found dismiss button, clicking it...');
    await dismissButton.click();
    await page.waitForTimeout(1000);
  }
  
  // Take screenshot after dismissing
  await page.screenshot({ path: 'screenshot-3-after-dismiss.png', fullPage: true });
  
  // Look for tabs - the chat interface might be on a different tab
  console.log('Looking for tabs in CopilotKit panel...');
  const tabs = await page.$$('[role="tab"], button[class*="tab" i]');
  console.log(`Found ${tabs.length} potential tabs`);
  
  for (let i = 0; i < tabs.length; i++) {
    const tab = tabs[i];
    const text = await tab.textContent().catch(() => '');
    const isVisible = await tab.isVisible();
    console.log(`  Tab ${i}: "${text}", visible=${isVisible}`);
  }
  
  // Try clicking on different areas to find the chat
  console.log('Looking for chat/message area...');
  
  // Try to click outside the event log to get to the main chat
  await page.evaluate(() => {
    // Look for elements that might contain the chat input
    const elements = document.querySelectorAll('[class*="copilot"]');
    console.log('Found copilot elements:', elements.length);
    for (const el of elements) {
      console.log(' - ', el.className, el.tagName);
    }
  });
  
  // Try to find chat input again - but exclude search boxes
  const inputSelectors = [
    '.copilotKitInput textarea',
    '.copilotKitInput > textarea',
    'textarea[placeholder*="message" i]',
    'textarea[placeholder*="chat" i]',
    'textarea[placeholder*="type" i]',
    'textarea[placeholder*="agent" i]',
    'input[type="text"][placeholder*="message" i]',
    'input[type="text"][placeholder*="chat" i]',
    '[contenteditable="true"]',
    '[aria-label*="chat" i][aria-label*="input" i]',
    'textarea:not([placeholder*="search" i])',
  ];
  
  console.log('Looking for chat input (excluding search boxes)...');
  
  // First try to find it normally
  for (const selector of inputSelectors) {
    try {
      const elements = await page.$$(selector);
      console.log(`Input selector ${selector}: found ${elements.length} elements`);
      
      for (let i = 0; i < elements.length; i++) {
        const element = elements[i];
        const isVisible = await element.isVisible();
        const placeholder = await element.getAttribute('placeholder').catch(() => '');
        const outerHTML = await element.evaluate(el => el.outerHTML.substring(0, 300)).catch(() => '');
        
        console.log(`  Input element ${i}: visible=${isVisible}, placeholder="${placeholder}"`);
        
        // Skip if it's the search findings input
        if (placeholder.toLowerCase().includes('search') && !placeholder.toLowerCase().includes('chat')) {
          console.log(`  Skipping search input`);
          continue;
        }
        
        if (isVisible) {
          console.log(`  Found potential chat input!`);
          chatInput = element;
          break;
        }
      }
      
      if (chatInput) break;
    } catch (e) {
      console.log(`  Error with input selector ${selector}: ${e.message}`);
    }
  }
  
  // If not found, try a more generic approach - find ALL textareas
  if (!chatInput) {
    console.log('Trying generic textarea search...');
    const allTextareas = await page.$$('textarea');
    console.log(`Found ${allTextareas.length} total textareas on page`);
    
    for (let i = 0; i < allTextareas.length; i++) {
      const textarea = allTextareas[i];
      const isVisible = await textarea.isVisible();
      const placeholder = await textarea.getAttribute('placeholder').catch(() => '');
      const id = await textarea.getAttribute('id').catch(() => '');
      const className = await textarea.getAttribute('class').catch(() => '');
      
      console.log(`  Textarea ${i}: visible=${isVisible}, placeholder="${placeholder}", id="${id}", class="${className}"`);
      
      // Skip search inputs
      if (placeholder && placeholder.toLowerCase().includes('search') && !placeholder.toLowerCase().includes('chat')) {
        console.log(`    Skipping search textarea`);
        continue;
      }
      
      if (isVisible) {
        console.log(`    Using this textarea!`);
        chatInput = textarea;
        break;
      }
    }
  }
  
  // Last resort: Use JavaScript to find the textarea
  if (!chatInput) {
    console.log('Trying JavaScript-based search for textarea...');
    const found = await page.evaluate(() => {
      // Look for textarea in the CopilotKit input
      const textareas = Array.from(document.querySelectorAll('textarea'));
      console.log('JS: Found textareas:', textareas.length);
      
      // Also check for elements with .copilotKitInput class
      const copilotInputs = Array.from(document.querySelectorAll('.copilotKitInput, [class*="copilot"]'));
      console.log('JS: Found copilot elements:', copilotInputs.length);
      
      // Check if textarea exists and is visible
      for (const textarea of textareas) {
        const rect = textarea.getBoundingClientRect();
        const isVisible = rect.width > 0 && rect.height > 0;
        console.log('JS: Textarea visible:', isVisible, 'placeholder:', textarea.placeholder);
        
        if (isVisible && (!textarea.placeholder || !textarea.placeholder.toLowerCase().includes('search'))) {
          return { found: true, placeholder: textarea.placeholder, classList: textarea.className };
        }
      }
      
      return { found: false, textareaCount: textareas.length };
    });
    
    console.log('JavaScript search result:', JSON.stringify(found));
    
    if (found.found) {
      // Try to select it again now that we know it exists
      chatInput = await page.$('textarea');
    }
  }
  
  if (chatInput) {
    console.log('Step 5: Found chat input! Typing message...');
    await chatInput.click();
    await chatInput.fill('What tools do you have access to? List all of them including MCP tools.');
    
    console.log('Step 6: Taking screenshot with typed message...');
    await page.screenshot({ path: 'screenshot-3-message-typed.png', fullPage: true });
    
    console.log('Step 7: Sending message...');
    
    // Try to find and click send button
    const sendSelectors = [
      'button[type="submit"]',
      'button[aria-label*="send" i]',
      'button:has-text("Send")',
      '[data-testid*="send" i]',
    ];
    
    let sendButton = null;
    for (const selector of sendSelectors) {
      try {
        sendButton = await page.waitForSelector(selector, { timeout: 1000 });
        if (sendButton) {
          console.log(`Found send button with selector: ${selector}`);
          break;
        }
      } catch (e) {
        // Continue to next selector
      }
    }
    
    if (sendButton) {
      await sendButton.click();
    } else {
      // Try pressing Enter
      console.log('No send button found, trying Enter key...');
      await chatInput.press('Enter');
    }
    
    console.log('Step 8: Waiting for AI response (20 seconds)...');
    await page.waitForTimeout(20000);
    
    console.log('Step 9: Taking screenshot of response...');
    await page.screenshot({ path: 'screenshot-4-response.png', fullPage: true });
    
    // Try to extract the response text
    console.log('\nStep 10: Attempting to extract response text...');
    const pageText = await page.evaluate(() => document.body.innerText);
    
    // Save the full page text
    fs.writeFileSync('page-text.txt', pageText);
    console.log('Full page text saved to page-text.txt');
    
    // Look for tool mentions in the response
    const toolKeywords = [
      'search_findings',
      'list_mcp_tools',
      'call_mcp_tool',
      'get_mcp_connection_status',
      'navigateToFinding',
      'renderFindingsResults',
      'MCP',
      'tools',
    ];
    
    console.log('\n=== ANALYSIS ===');
    console.log('Checking for tool-related keywords in response:');
    for (const keyword of toolKeywords) {
      const found = pageText.toLowerCase().includes(keyword.toLowerCase());
      console.log(`  ${keyword}: ${found ? '✓ FOUND' : '✗ Not found'}`);
    }
    
  } else {
    console.log('ERROR: Could not find chat input field!');
    console.log('Taking final screenshot...');
    await page.screenshot({ path: 'screenshot-error.png', fullPage: true });
    
    // Get page HTML for debugging
    const html = await page.content();
    fs.writeFileSync('page-html.txt', html);
    console.log('Page HTML saved to page-html.txt for debugging');
  }
  
  console.log('\n=== TEST COMPLETE ===');
  console.log('Screenshots saved. Press Ctrl+C to close browser or wait 10 seconds...');
  await page.waitForTimeout(10000);
  
  await browser.close();
})();
