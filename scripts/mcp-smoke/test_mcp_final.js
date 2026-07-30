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

  console.log('=== COPILOTKIT MCP CONNECTION TEST ===\n');

  try {
    console.log('Step 1: Navigating to http://127.0.0.1:6988...');
    await page.goto('http://127.0.0.1:6988', { waitUntil: 'networkidle', timeout: 10000 });
    
    console.log('Step 2: Waiting 4 seconds for page to load...');
    await page.waitForTimeout(4000);
    await page.screenshot({ path: 'mcp-test-1-loaded.png', fullPage: true });
    
    console.log('Step 3: Looking for CopilotKit chat button...');
    const chatButton = await page.$('button[aria-label="Vigil Chat"]');
    if (!chatButton) {
      console.log('ERROR: Chat button not found!');
      await page.screenshot({ path: 'mcp-test-error.png', fullPage: true });
      await browser.close();
      return;
    }
    
    console.log('Found chat button, opening chat...');
    await chatButton.evaluate(el => el.click());
    await page.waitForTimeout(2000);
    
    // Dismiss any announcement
    const dismissBtn = await page.$('button:has-text("Dismiss")');
    if (dismissBtn) {
      await dismissBtn.click();
      await page.waitForTimeout(500);
    }
    
    await page.screenshot({ path: 'mcp-test-2-chat-opened.png', fullPage: true });
    
    console.log('Step 4: Finding textarea and typing query...');
    const textarea = await page.$('textarea[placeholder*="analyst" i]');
    if (!textarea) {
      console.log('ERROR: Textarea not found!');
      await browser.close();
      return;
    }
    
    const query = 'Check the MCP connection status';
    console.log(`Typing: "${query}"`);
    await textarea.click();
    await textarea.fill(query);
    await page.screenshot({ path: 'mcp-test-3-query-typed.png', fullPage: true });
    
    console.log('Step 5: Sending message (pressing Enter)...');
    await textarea.press('Enter');
    
    console.log('Step 6: Waiting 30 seconds for response...');
    
    // Take screenshots at intervals
    for (let i = 5; i <= 30; i += 5) {
      await page.waitForTimeout(5000);
      console.log(`  ${i} seconds...`);
      await page.screenshot({ path: `mcp-test-4-response-${i}s.png`, fullPage: true });
    }
    
    console.log('\nStep 7: Analyzing response...');
    
    // Get the full page text
    const fullText = await page.evaluate(() => document.body.innerText);
    fs.writeFileSync('mcp-test-full-text.txt', fullText);
    
    // Extract chat messages more carefully
    const chatContent = await page.evaluate(() => {
      // Look for the CopilotKit sidebar content
      const sidebar = document.querySelector('[class*="copilot"]');
      if (sidebar) {
        return sidebar.innerText;
      }
      return '';
    });
    
    fs.writeFileSync('mcp-test-chat-content.txt', chatContent);
    
    // Analysis
    const serverNames = [
      'deeptempo-findings',
      'tempo-flow', 
      'approval',
      'attack-layer',
      'github',
      'crowdstrike',
      'sentinelone',
      'security-detections'
    ];
    
    const foundServers = serverNames.filter(name => 
      fullText.toLowerCase().includes(name.toLowerCase())
    );
    
    const hasConnectionCount = /(\d+)\s+(connected|total)\s+server/i.test(fullText);
    const hasToolCall = fullText.toLowerCase().includes('calling') || 
                        fullText.toLowerCase().includes('executing') ||
                        fullText.toLowerCase().includes('tool');
    const hasError = fullText.toLowerCase().includes('error') ||
                     fullText.toLowerCase().includes('failed') ||
                     fullText.toLowerCase().includes('cannot access') ||
                     fullText.toLowerCase().includes('don\'t have access');
    
    console.log('\n=== ANALYSIS RESULTS ===');
    console.log('Tool call detected:', hasToolCall ? '✓ YES' : '✗ NO');
    console.log('Connection count mentioned:', hasConnectionCount ? '✓ YES' : '✗ NO');
    console.log('Error messages:', hasError ? '✓ YES (ERROR)' : '✗ NO');
    console.log('\nSpecific MCP servers mentioned:');
    if (foundServers.length > 0) {
      foundServers.forEach(server => console.log(`  ✓ ${server}`));
    } else {
      console.log('  ✗ None found');
    }
    
    // Look for specific patterns
    const patterns = {
      'connected servers': /(\d+)\s+connected\s+server/i,
      'total servers': /(\d+)\s+total\s+server/i,
      'MCP': /MCP/,
      'connection status': /connection\s+status/i,
    };
    
    console.log('\nPattern matches:');
    for (const [name, pattern] of Object.entries(patterns)) {
      const match = fullText.match(pattern);
      if (match) {
        console.log(`  ✓ "${name}": ${match[0]}`);
      } else {
        console.log(`  ✗ "${name}": not found`);
      }
    }
    
    // Extract just the AI response portion
    const aiResponseMatch = chatContent.match(/Check the MCP connection status\s*(.+?)(?:Ask the SOC|$)/s);
    if (aiResponseMatch) {
      const aiResponse = aiResponseMatch[1].trim().substring(0, 500);
      console.log('\n=== AI RESPONSE ===');
      console.log(aiResponse);
      console.log(aiResponse.length > 500 ? '... (truncated)' : '');
    }
    
    console.log('\n=== CONCLUSION ===');
    if (foundServers.length > 0 && hasConnectionCount) {
      console.log('✅ SUCCESS: AI returned real MCP server data');
    } else if (foundServers.length > 0) {
      console.log('⚠️  PARTIAL: AI mentioned servers but no counts');
    } else if (hasError) {
      console.log('❌ FAILED: AI reported error accessing MCP tools');
    } else {
      console.log('❌ FAILED: AI did not return MCP server data');
    }
    
    console.log('\nScreenshots saved: mcp-test-*.png');
    console.log('Full text saved: mcp-test-full-text.txt');
    console.log('Chat content saved: mcp-test-chat-content.txt');
    
    console.log('\nWaiting 10 seconds before closing...');
    await page.waitForTimeout(10000);
    
  } catch (error) {
    console.error('ERROR:', error.message);
    await page.screenshot({ path: 'mcp-test-error.png', fullPage: true });
  } finally {
    await browser.close();
  }
})();
