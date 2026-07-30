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

  console.log('=== TESTING COPILOTKIT WITH list_mcp_tools ===\n');

  try {
    console.log('Step 1: Navigating to http://localhost:5173...');
    await page.goto('http://localhost:5173', { waitUntil: 'networkidle', timeout: 15000 });
    
    console.log('Step 2: Taking initial snapshot...');
    await page.waitForTimeout(3000);
    await page.screenshot({ path: 'localhost-test-1-initial.png', fullPage: true });
    
    console.log('Step 3: Looking for CopilotKit chat interface...');
    
    // Look for chat button or sidebar
    const chatButton = await page.$('button[aria-label*="chat" i], button[aria-label*="copilot" i]');
    if (chatButton) {
      console.log('Found chat button, clicking to open...');
      await chatButton.evaluate(el => el.click());
      await page.waitForTimeout(2000);
    } else {
      console.log('No chat button found - sidebar may already be open');
    }
    
    await page.screenshot({ path: 'localhost-test-2-chat-opened.png', fullPage: true });
    
    // Dismiss any announcements
    const dismissBtn = await page.$('button:has-text("Dismiss")');
    if (dismissBtn) {
      console.log('Dismissing announcement...');
      await dismissBtn.click();
      await page.waitForTimeout(500);
    }
    
    console.log('Step 4: Finding chat textarea...');
    const textarea = await page.$('textarea, input[type="text"][placeholder*="message" i], [contenteditable="true"]');
    
    if (!textarea) {
      console.log('ERROR: Could not find chat input!');
      await page.screenshot({ path: 'localhost-test-error-no-input.png', fullPage: true });
      
      // Try to get all textareas
      const allTextareas = await page.$$('textarea');
      console.log(`Found ${allTextareas.length} textareas on page`);
      
      await browser.close();
      return;
    }
    
    const testMessage = 'What MCP tools are available? Use the list_mcp_tools tool to find out.';
    console.log(`Step 5: Typing message: "${testMessage}"`);
    await textarea.click();
    await textarea.fill(testMessage);
    await page.screenshot({ path: 'localhost-test-3-message-typed.png', fullPage: true });
    
    console.log('Step 6: Sending message...');
    await textarea.press('Enter');
    
    console.log('Step 7: Waiting for response (checking every 5 seconds for 30 seconds)...\n');
    
    for (let i = 5; i <= 30; i += 5) {
      await page.waitForTimeout(5000);
      console.log(`  ${i} seconds elapsed...`);
      await page.screenshot({ path: `localhost-test-4-response-${i}s.png`, fullPage: true });
      
      // Check if response has appeared
      const pageText = await page.evaluate(() => document.body.innerText);
      if (pageText.includes('list_mcp_tools') || pageText.includes('deeptempo') || pageText.includes('tool')) {
        console.log(`  → Response detected at ${i} seconds!`);
      }
    }
    
    console.log('\nStep 8: Extracting and analyzing response...\n');
    
    const fullText = await page.evaluate(() => document.body.innerText);
    fs.writeFileSync('localhost-test-full-text.txt', fullText);
    
    // Extract chat content
    const chatContent = await page.evaluate(() => {
      const sidebar = document.querySelector('[class*="copilot"], [class*="chat"]');
      return sidebar ? sidebar.innerText : document.body.innerText;
    });
    fs.writeFileSync('localhost-test-chat-content.txt', chatContent);
    
    // Analysis
    const toolCallPattern = /list_mcp_tools|calling.*tool|executing|function call/i;
    const hasToolCall = toolCallPattern.test(fullText);
    
    const mcpServerNames = [
      'deeptempo-findings',
      'tempo-flow',
      'approval',
      'attack-layer',
      'search_findings',
      'get_finding_details',
      'create_case',
      'analyze_with_agent'
    ];
    
    const foundTools = mcpServerNames.filter(name => 
      fullText.toLowerCase().includes(name.toLowerCase())
    );
    
    const hasError = /error|failed|cannot|don't have access|no access/i.test(chatContent);
    
    console.log('=== ANALYSIS ===');
    console.log('Tool call detected (list_mcp_tools):', hasToolCall ? '✓ YES' : '✗ NO');
    console.log('Error in response:', hasError ? '✓ YES (ERROR)' : '✗ NO');
    console.log('\nMCP tools/servers found in response:');
    if (foundTools.length > 0) {
      foundTools.forEach(tool => console.log(`  ✓ ${tool}`));
    } else {
      console.log('  ✗ None found');
    }
    
    // Extract AI response
    const messageMatch = chatContent.match(/What MCP tools are available.*?\n\n([\s\S]+?)(?:\n\nPowered by|Ask the|$)/);
    let aiResponse = 'Could not extract response';
    
    if (messageMatch) {
      aiResponse = messageMatch[1].trim();
    } else {
      // Try to find any response after our message
      const lines = chatContent.split('\n');
      const queryIndex = lines.findIndex(line => line.includes('What MCP tools are available'));
      if (queryIndex !== -1 && queryIndex < lines.length - 1) {
        aiResponse = lines.slice(queryIndex + 1, queryIndex + 20).join('\n').trim();
      }
    }
    
    console.log('\n=== AI RESPONSE ===');
    console.log(aiResponse);
    console.log('\n==================\n');
    
    // Check for specific indicators
    const indicators = {
      toolExecuted: hasToolCall,
      realDataReturned: foundTools.length > 0,
      errorShown: hasError,
      mentionsListMcpTools: fullText.toLowerCase().includes('list_mcp_tools'),
      mentionsMCP: fullText.toLowerCase().includes('mcp'),
    };
    
    console.log('=== INDICATORS ===');
    console.log('Tool execution:', indicators.toolExecuted ? '✓ YES' : '✗ NO');
    console.log('Real MCP data returned:', indicators.realDataReturned ? '✓ YES' : '✗ NO');  
    console.log('Errors present:', indicators.errorShown ? '✓ YES' : '✗ NO');
    console.log('Mentions "list_mcp_tools":', indicators.mentionsListMcpTools ? '✓ YES' : '✗ NO');
    console.log('Mentions "MCP":', indicators.mentionsMCP ? '✓ YES' : '✗ NO');
    
    console.log('\n=== CONCLUSION ===');
    if (indicators.realDataReturned && indicators.toolExecuted) {
      console.log('✅ SUCCESS: list_mcp_tools was called and returned real data');
    } else if (indicators.mentionsListMcpTools && !indicators.realDataReturned) {
      console.log('⚠️  PARTIAL: AI mentioned the tool but did not return data');
    } else if (indicators.errorShown) {
      console.log('❌ FAILED: Error occurred when trying to access MCP tools');
    } else {
      console.log('❌ FAILED: AI did not call list_mcp_tools or return MCP data');
    }
    
    console.log('\nFiles saved:');
    console.log('  - localhost-test-*.png (screenshots)');
    console.log('  - localhost-test-full-text.txt');
    console.log('  - localhost-test-chat-content.txt');
    
    console.log('\nWaiting 10 seconds before closing...');
    await page.waitForTimeout(10000);
    
  } catch (error) {
    console.error('\nERROR:', error.message);
    await page.screenshot({ path: 'localhost-test-error.png', fullPage: true });
  } finally {
    await browser.close();
  }
})();
