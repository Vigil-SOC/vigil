const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ 
    headless: false,
    args: ['--no-sandbox', '--disable-setuid-sandbox']
  });
  const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 }
  });
  const page = await context.newPage();

  console.log('=== TESTING MCP CONNECTION STATUS ===\n');

  console.log('Step 1: Navigating to http://127.0.0.1:6988...');
  await page.goto('http://127.0.0.1:6988', { waitUntil: 'networkidle' });
  await page.waitForTimeout(3000);
  
  console.log('Step 2: Taking screenshot of initial page...');
  await page.screenshot({ path: 'test-mcp-1-initial.png', fullPage: true });
  
  console.log('Step 3: Looking for chat button...');
  const chatButton = await page.$('button[aria-label="Vigil Chat"]');
  if (chatButton) {
    console.log('Found chat button, clicking...');
    await chatButton.evaluate(el => el.click());
    await page.waitForTimeout(2000);
  } else {
    console.log('ERROR: Chat button not found!');
    await page.screenshot({ path: 'test-mcp-error.png', fullPage: true });
    await browser.close();
    return;
  }
  
  console.log('Step 4: Taking screenshot after opening chat...');
  await page.screenshot({ path: 'test-mcp-2-chat-opened.png', fullPage: true });
  
  // Dismiss announcement if present
  const dismissBtn = await page.$('button:has-text("Dismiss")');
  if (dismissBtn) {
    console.log('Dismissing announcement...');
    await dismissBtn.click();
    await page.waitForTimeout(1000);
  }
  
  console.log('Step 5: Finding textarea and typing MCP connection status query...');
  const textarea = await page.$('textarea[placeholder*="analyst" i], textarea[placeholder*="ask" i]');
  
  if (!textarea) {
    console.log('ERROR: Could not find textarea');
    await page.screenshot({ path: 'test-mcp-error.png', fullPage: true });
    await browser.close();
    return;
  }
  
  const testMessage = 'Check the MCP connection status and tell me which servers are connected';
  console.log(`Typing: "${testMessage}"`);
  await textarea.click();
  await textarea.fill(testMessage);
  
  console.log('Step 6: Taking screenshot with message typed...');
  await page.screenshot({ path: 'test-mcp-3-message-typed.png', fullPage: true });
  
  console.log('Step 7: Pressing Enter to send...');
  await textarea.press('Enter');
  
  // Take screenshots at intervals to capture the response as it comes in
  console.log('Step 8: Waiting for AI response...');
  
  await page.waitForTimeout(5000);
  console.log('  5 seconds - taking screenshot...');
  await page.screenshot({ path: 'test-mcp-4-response-5s.png', fullPage: true });
  
  await page.waitForTimeout(5000);
  console.log('  10 seconds - taking screenshot...');
  await page.screenshot({ path: 'test-mcp-5-response-10s.png', fullPage: true });
  
  await page.waitForTimeout(10000);
  console.log('  20 seconds - taking screenshot...');
  await page.screenshot({ path: 'test-mcp-6-response-20s.png', fullPage: true });
  
  await page.waitForTimeout(10000);
  console.log('  30 seconds - taking final screenshot...');
  await page.screenshot({ path: 'test-mcp-7-response-final.png', fullPage: true });
  
  console.log('\nStep 9: Extracting chat content...');
  
  // Get all text from the page
  const fullPageText = await page.evaluate(() => document.body.innerText);
  
  // Look for specific indicators
  const indicators = {
    toolCalls: fullPageText.toLowerCase().includes('calling') || 
               fullPageText.toLowerCase().includes('tool') ||
               fullPageText.toLowerCase().includes('function'),
    mcpServers: fullPageText.toLowerCase().includes('servers') ||
                fullPageText.toLowerCase().includes('connected'),
    serverNames: {
      'deeptempo-findings': fullPageText.includes('deeptempo-findings'),
      'tempo-flow': fullPageText.includes('tempo-flow'),
      'approval': fullPageText.includes('approval'),
      'attack-layer': fullPageText.includes('attack-layer'),
    },
    noAccess: fullPageText.toLowerCase().includes('don\'t have access') ||
              fullPageText.toLowerCase().includes('cannot access') ||
              fullPageText.toLowerCase().includes('no access'),
    error: fullPageText.toLowerCase().includes('error') ||
           fullPageText.toLowerCase().includes('failed'),
  };
  
  console.log('\n=== ANALYSIS ===');
  console.log('Tool call indicators:', indicators.toolCalls ? '✓ YES' : '✗ NO');
  console.log('Mentions servers:', indicators.mcpServers ? '✓ YES' : '✗ NO');
  console.log('Mentions "no access":', indicators.noAccess ? '✓ YES' : '✗ NO');
  console.log('Shows error:', indicators.error ? '✓ YES' : '✗ NO');
  console.log('\nSpecific server names found:');
  for (const [server, found] of Object.entries(indicators.serverNames)) {
    console.log(`  ${server}: ${found ? '✓ FOUND' : '✗ Not found'}`);
  }
  
  // Extract visible messages
  const messages = await page.evaluate(() => {
    const msgElements = Array.from(document.querySelectorAll('[class*="message" i], [role="log"]'));
    return msgElements.map(el => el.textContent?.substring(0, 300) || '').filter(t => t.length > 10);
  });
  
  console.log('\n=== CHAT MESSAGES ===');
  messages.forEach((msg, i) => {
    console.log(`Message ${i + 1}: ${msg.substring(0, 150)}...`);
  });
  
  // Search for specific keywords in the full page text
  const keywords = [
    'get_mcp_connection_status',
    'list_mcp_tools',
    'call_mcp_tool',
    'connected',
    'servers',
    'MCP',
    'connection',
    'status',
  ];
  
  console.log('\n=== KEYWORD DETECTION ===');
  keywords.forEach(keyword => {
    const found = fullPageText.toLowerCase().includes(keyword.toLowerCase());
    console.log(`  "${keyword}": ${found ? '✓ FOUND' : '✗ Not found'}`);
  });
  
  console.log('\n=== TEST COMPLETE ===');
  console.log('Screenshots saved: test-mcp-*.png');
  console.log('Waiting 10 seconds before closing...');
  await page.waitForTimeout(10000);
  
  await browser.close();
})();
