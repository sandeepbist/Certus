'use client';

import { useState, useEffect } from 'react';
import { Sun, Moon } from 'lucide-react';

export function ThemeToggle() {
  const [theme, setTheme] = useState<'dark' | 'light'>('dark');

  useEffect(() => {
    const savedTheme = (localStorage.getItem('certus_theme') as 'dark' | 'light') || 'dark';
    setTheme(savedTheme);
    document.documentElement.setAttribute('data-theme', savedTheme);
  }, []);

  const toggleTheme = () => {
    const nextTheme = theme === 'dark' ? 'light' : 'dark';
    setTheme(nextTheme);
    localStorage.setItem('certus_theme', nextTheme);
    document.documentElement.setAttribute('data-theme', nextTheme);
  };

  return (
    <button
      onClick={toggleTheme}
      aria-label="Toggle visual theme"
      className="w-8 h-8 rounded-lg flex items-center justify-center text-zinc-400 hover:text-white bg-zinc-900 hover:bg-zinc-800 border border-zinc-800 transition-colors"
    >
      {theme === 'dark' ? (
        <Sun className="w-3.5 h-3.5 text-zinc-300" />
      ) : (
        <Moon className="w-3.5 h-3.5 text-zinc-300" />
      )}
    </button>
  );
}
