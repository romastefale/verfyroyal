import express from 'express';
import { loadSettings, startBotWorker } from './bot.js';

const app = express();
const port = process.env.PORT || 3000;

app.get('/', (req, res) => {
  res.send('Telegram Verifier Bot is running');
});

app.listen(port, '0.0.0.0', () => {
  console.log(`[Web] Listening on port ${port}`);
  
  try {
    const settings = loadSettings();
    startBotWorker(settings);
  } catch (err) {
    console.error('[Web] Failed to start bot:', err.message);
  }
});
