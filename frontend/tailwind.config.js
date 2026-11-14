/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        cream: '#fbf5e6', paper: '#fffdf6', mint: '#bfe8d6', sun: '#ffe9a8', sky: '#cfe8f7',
        coral: '#f6a192', brown: '#7a5a3c', ink: '#4a3a2a',
      },
      boxShadow: { px: '3px 3px 0 #7a5a3c', pxs: '2px 2px 0 #7a5a3c' },
    },
  },
  plugins: [],
}
