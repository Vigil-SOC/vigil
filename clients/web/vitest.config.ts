import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    // shell.css is read as text by CommandBar.test.tsx, which asserts the disabled treatment exists
    css: { include: [/shell\.css/] },
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
    // Don't pick up test files inside agent worktrees — those copies of
    // the repo lack node_modules and crash the run with "Cannot find
    // package '@testing-library/react'". The .claude directory sits two
    // levels up from clients/web/ so we need both relative-up and bare-glob
    // patterns to catch it. Vitest's default excludes skip node_modules
    // and .git only.
    exclude: [
      '**/node_modules/**',
      '**/.git/**',
      '**/.claude/**',
      '../../.claude/**',
      '**/worktrees/**',
    ],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json', 'html', 'lcov'],
      exclude: [
        'node_modules/',
        'src/test/',
        '**/*.test.tsx',
        '**/*.test.ts',
        'src/vite-env.d.ts',
      ],
    },
  },
});
