import { FlatCompat } from '@eslint/eslintrc';
import tseslint from 'typescript-eslint';

const compat = new FlatCompat({
  baseDirectory: import.meta.dirname,
});

const webConfig = compat
  .extends('next/core-web-vitals', 'next/typescript')
  .map((config) => ({
    ...config,
    files: ['services/web/**/*.{js,jsx,ts,tsx}'],
  }));

export default tseslint.config(
  {
    ignores: [
      '**/.next/**',
      '**/dist/**',
      '**/node_modules/**',
      '**/generated/**',
    ],
  },
  ...webConfig,
  {
    files: ['services/web/**/*.{js,jsx,ts,tsx}'],
    rules: {
      '@next/next/no-html-link-for-pages': 'off',
      '@typescript-eslint/no-explicit-any': 'off',
    },
  },
  {
    files: ['services/gateway/**/*.ts', 'scripts/**/*.ts', 'tests/**/*.ts'],
    extends: [...tseslint.configs.recommended],
    rules: {
      '@typescript-eslint/no-explicit-any': 'off',
      '@typescript-eslint/no-unused-vars': [
        'warn',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
    },
  },
);
