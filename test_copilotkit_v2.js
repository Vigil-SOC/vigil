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

  console.log('Step 1: Navigating to http://127.0.0.1:6988...');
  await page.goto('http://127.0.0.1:6988');
  await page.waitForTimeout(3000);
  
  console.log('Step 2: Taking screenshot of initial page...');
  await page.screenshot({ path: 'screenshot-1-initial.png', fullPage: true });
  
  console.log('Step 3: Looking for and clicking chat button...');
  
  // Find and click the chat button
  const chatButton = await page.$('button[aria-label="Vigil Chat"]');
  if (chatButton) {
    console.log('Found chat button, clicking with force...');
    await chatButton.evaluate(el => el.click());
    await page.waitForTimeout(3000);
  } else {
    console.log('ERROR: Chat button not found!');
    await browser.close();
    return;
  }
  
  console.log('Step 4: Taking screenshot after opening chat...');
  await page.screenshot({ path: 'screenshot-2-chat-opened.png', fullPage: true });
  
  console.log('Step 5: Dismissing announcement if present...');
  const dismissBtn = await page.$('button:has-text("Dismiss")');
  if (dismissBtn) {
    await dismissBtn.click();
    await page.waitForTimeout(1000);
  }
  
  console.log('Step 6: Finding textarea and typing message with Playwright...');
  
  // Wait for the textarea to be available
  await page.waitForTimeout(2000);
  
  // Use Playwright to directly type into the textarea
  const textarea = await page.$('textarea[placeholder*="analyst" i], textarea[placeholder*="ask" i]');
  
  if (!textarea) {
    console.log('ERROR: Could not find textarea with Playwright selector');
    await browser.close();
    return;
  }
  
  console.log('Found textarea with Playwright, clicking and typing...');
  await textarea.click();
  await textarea.fill('What tools do you have access to? List all of them including MCP tools.');
  
  console.log('Step 7: Taking screenshot with typed message...');
  await page.screenshot({ path: 'screenshot-3-message-typed.png', fullPage: true });
  
  console.log('Step 8: Pressing Enter to send...');
  await textarea.press('Enter');
  
  console.log('Step 9: Waiting for AI response (40 seconds)...');
  await page.waitForTimeout(40000);
  
  console.log('Step 10: Taking screenshot of response...');
  await page.screenshot({ path: 'screenshot-4-response.png', fullPage: true });
  
  // Extract text from the chat
  const chatText = await page.evaluate(() => {
    // Look for message containers
    const messages = document.querySelectorAll('[class*="message" i], [class*="chat" i], [role="log"]');
    const texts = [];
    
    for (const msg of messages) {
      const text = msg.textContent;
      if (text && text.length > 20) {
        texts.push(text.substring(0, 500));
      }
    }
    
    return texts;
  });
  
  console.log('\n=== CHAT MESSAGES ===');
  console.log(JSON.stringify(chatText, null, 2));
  
  // Save to file
  fs.writeFileSync('chat-messages.json', JSON.stringify(chatText, null, 2));
  
  // Check for tool mentions
  const fullPageText = await page.evaluate(() => document.body.innerText);
  fs.writeFileSync('page-text.txt', fullPageText);
  
  const toolKeywords = [
    'search_findings',
    'list_mcp_tools',
    'call_mcp_tool',
    'get_mcp_connection_status',
    'navigateToFinding',
    'renderFindingsResults',
    'switchAgent',
    'MCP',
  ];
  
  console.log('\n=== TOOL DETECTION ===');
  for (const keyword of toolKeywords) {
    const found = fullPageText.toLowerCase().includes(keyword.toLowerCase());
    console.log(`  ${keyword}: ${found ? '✓ FOUND' : '✗ Not found'}`);
  }
  
  console.log('\n=== TEST COMPLETE ===');
  console.log('Waiting 10 seconds before closing...');
  await page.waitForTimeout(10000);
  
  await browser.close();
})();
