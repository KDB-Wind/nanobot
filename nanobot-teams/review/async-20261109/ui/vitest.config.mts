import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const review = path.dirname(fileURLToPath(import.meta.url));
const app = path.resolve(review, '../../../../webui');
const fromApp = createRequire(path.join(app, 'package.json'));
export default {
  root: app,
  esbuild: { jsx: 'automatic' },
  resolve: {
    alias: [
      { find: '@', replacement: app + '/src' },
      { find: /^vitest$/, replacement: app + '/node_modules/vitest/dist/index.js' },
      { find: /^@testing-library\/react$/, replacement: fromApp.resolve('@testing-library/react') },
      { find: /^react$/, replacement: fromApp.resolve('react') },
      { find: 'react/jsx-runtime', replacement: fromApp.resolve('react/jsx-runtime') },
    ],
    dedupe: ['react', 'react-dom'],
  },
  test: {
    environment: 'happy-dom', globals: true,
    setupFiles: [app + '/src/tests/setup.ts'],
    include: [(review + '/submission.test.tsx').replaceAll('\\', '/')],
    maxWorkers: 1, minWorkers: 1,
  },
};
