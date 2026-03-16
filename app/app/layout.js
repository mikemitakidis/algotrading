export const metadata = {
  title: 'Algo Trader v2.0',
  description: 'Multi-timeframe algorithmic trading dashboard',
}

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet" />
      </head>
      <body style={{ margin: 0, padding: 0, background: '#0d1117' }}>
        {children}
      </body>
    </html>
  )
}
