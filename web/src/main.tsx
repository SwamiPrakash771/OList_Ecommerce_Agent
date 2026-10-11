import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import './index.css'
import { ChatProvider } from './chat'
import { StoreProvider } from './state'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <StoreProvider>
        <ChatProvider>
          <App />
        </ChatProvider>
      </StoreProvider>
    </BrowserRouter>
  </StrictMode>,
)
