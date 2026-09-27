const { ipcRenderer } = require('electron');

document.getElementById('toggleBtn').addEventListener('click', () => {
  ipcRenderer.send('toggle-drawer');
});
