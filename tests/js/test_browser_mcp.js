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

  console.log('=== BROWSER TEST: "list mcp tools" ===\n');

  try {
    console.log('Step 1: Navigating to http://127.0.0.1:6988...');
    await page.goto('http://127.0.0.1:6988', { waitUntil: 'networkidle', timeout: 15000 });
    await page.waitForTimeout(2000);
    
    console.log('✓ Page loaded successfully\n');
    
    console.log('Step 2: Taking screenshot of loaded page...');
    await page.screenshot({ path: 'browser-test-1-page-loaded.png', fullPage: true });
    
    const pageTitle = await page.title();
    console.log(`Page title: "${pageTitle}"`);
    
    console.log('\nStep 3: Looking for CopilotKit chat interface...');
    
    // Try to find chat button
    const chatButton = await page.$('button[aria-label*="chat" i], button[aria-label*="copilot" i]');
    
    if (chatButton) {
      const label = await chatButton.getAttribute('aria-label');
      console.log(`✓ Found chat button: "${label}"`);
      console.log('Opening chat...');
      await chatButton.evaluate(el => el.click());
      await page.waitForTimeout(2000);
      console.log('✓ Chat opened\n');
    } else {
      console.log('⚠ No chat button found - checking if sidebar is already visible...');
    }
    
    console.log('Step 4: Taking screenshot with chat interface...');
    await page.screenshot({ path: 'browser-test-2-chat-opened.png', fullPage: true });
    
    // Dismiss announcement if present
    const dismissBtn = await page.$('button:has-text("Dismiss")');
    if (dismissBtn) {
      console.log('Dismissing announcement...');
      await dismissBtn.click();
      await page.waitForTimeout(500);
    }
    
    console.log('\nStep 5: Finding chat input...');
    const textarea = await page.$('textarea, input[placeholder*="analyst" i], input[placeholder*="ask" i]');
    
    if (!textarea) {
      console.log('❌ ERROR: Could not find chat input!');
      
      // Debug: list all textareas
      const allTextareas = await page.$$('textarea');
      console.log(`Debug: Found ${allTextareas.length} textarea elements`);
      
      await page.screenshot({ path: 'browser-test-error-no-input.png', fullPage: true });
      await browser.close();
      return;
    }
    
    console.log('✓ Found chat input\n');
    
    const message = 'list mcp tools';
    console.log(`Step 6: Typing message: "${message}"`);
    await textarea.click();
    await textarea.fill(message);
    await page.screenshot({ path: 'browser-test-3-message-typed.png', fullPage: true });
    console.log('✓ Message typed\n');
    
    console.log('Step 7: Sending message...');
    await textarea.press('Enter');
    console.log('✓ Message sent\n');
    
    console.log('Step 8: Waiting for response (checking every 5 seconds for 45 seconds)...\n');
    
    let lastText = '';
    
    for (let elapsed = 5; elapsed <= 45; elapsed += 5) {
      await page.waitForTimeout(5000);
      
      const currentText = await page.evaluate(() => {
        const sidebar = document.querySelector('[class*="copilot"], [class*="chat"]');
        return sidebar ? sidebar.innerText : document.body.innerText;
      });
      
      const hasNewContent = currentText !== lastText;
      const icon = hasNewContent ? '📝' : '⏳';
      
      console.log(`${icon} ${elapsed}s elapsed${hasNewContent ? ' - new content detected!' : ''}`);
      
      await page.screenshot({ path: `browser-test-4-response-${elapsed}s.png`, fullPage: true });
      
      lastText = currentText;
      
      // Check for completion indicators
      if (currentText.includes('Powered by') || currentText.includes('Ask the SOC')) {
        console.log('✓ Response appears complete\n');
      }
    }
    
    console.log('Step 9: Extracting final response...\n');
    
    const chatContent = await page.evaluate(() => {
      const sidebar = document.querySelector('[class*="copilot"]');
      return sidebar ? sidebar.innerText : '';
    });
    
    fs.writeFileSync('browser-test-chat-content.txt', chatContent);
    
    const fullPageText = await page.evaluate(() => document.body.innerText);
    fs.writeFileSync('browser-test-full-page.txt', fullPageText);
    
    // Extract just the AI's response
    const responseMatch = chatContent.match(/list mcp tools\s*\n\s*(.+?)(?:\n\s*Powered by|\n\s*Ask the|$)/s);
    let aiResponse = 'Could not extract response';
    
    if (responseMatch) {
      aiResponse = responseMatch[1].trim();
    } else {
      // Fallback: get everything after our message
      const parts = chatContent.split('list mcp tools');
      if (parts.length > 1) {
        aiResponse = parts[1].substring(0, 800).trim();
      }
    }
    
    console.log('=================================');
    console.log('AI RESPONSE:');
    console.log('=================================');
    console.log(aiResponse);
    console.log('=================================\n');
    
    // Analysis
    const toolNames = [
      'search_findings',
      'get_finding_details',
      'list_mcp_tools',
      'call_mcp_tool',
      'get_mcp_connection_status',
      'create_case',
      'analyze_with_agent',
      'deeptempo-findings',
      'tempo-flow',
      'approval',
      'attack-layer'
    ];
    
    const foundTools = toolNames.filter(tool => 
      aiResponse.toLowerCase().includes(tool.toLowerCase()) ||
      chatContent.toLowerCase().includes(tool.toLowerCase())
    );
    
    const hasError = /error|failed|cannot|don't have|no access/i.test(aiResponse);
    const hasToolCall = /calling|executing|tool|function/i.test(chatContent);
    
    console.log('ANALYSIS:');
    console.log('─────────');
    console.log('Response length:', aiResponse.length, 'characters');
    console.log('Tool call indicators:', hasToolCall ? '✓ YES' : '✗ NO');
    console.log('Error messages:', hasError ? '✓ YES (ERROR)' : '✗ NO');
    console.log('Tool names found:', foundTools.length > 0 ? `✓ YES (${foundTools.length})` : '✗ NO');
    
    if (foundTools.length > 0) {
      console.log('\nTools mentioned:');
      foundTools.forEach(tool => console.log(`  • ${tool}`));
    }
    
    console.log('\n=================================');
    console.log('SUMMARY:');
    console.log('=================================');
    console.log('1. Page loaded:', '✓ YES');
    console.log('2. CopilotKit visible:', chatButton ? '✓ YES' : '⚠ ASSUMED YES');
    console.log('3. Response received:', aiResponse.length > 20 ? '✓ YES' : '✗ NO');
    console.log('4. MCP tools listed:', foundTools.length > 0 ? '✓ YES' : '❌ NO');
    console.log('5. Errors present:', hasError ? '❌ YES' : '✓ NO');
    console.log('=================================\n');
    
    if (foundTools.length > 0 && !hasError) {
      console.log('✅ SUCCESS: AI returned MCP tool information!');
    } else if (hasError) {
      console.log('❌ FAILED: Error occurred accessing MCP tools');
    } else {
      console.log('❌ FAILED: AI did not list MCP tools');
    }
    
    console.log('\nFiles saved:');
    console.log('  • browser-test-*.png (screenshots)');
    console.log('  • browser-test-chat-content.txt');
    console.log('  • browser-test-full-page.txt');
    
    console.log('\nWaiting 10 seconds before closing browser...');
    await page.waitForTimeout(10000);
    
  } catch (error) {
    console.error('\n❌ ERROR:', error.message);
    await page.screenshot({ path: 'browser-test-error.png', fullPage: true });
  } finally {
    await browser.close();
  }
})();
