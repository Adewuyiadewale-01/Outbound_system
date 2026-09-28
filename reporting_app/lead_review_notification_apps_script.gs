const NOTIFICATION_QUEUE_TAB = 'Notification Queue';

function sendPendingLeadReviewNotifications() {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(NOTIFICATION_QUEUE_TAB);
  if (!sheet) {
    return;
  }

  const values = sheet.getDataRange().getValues();
  if (values.length < 2) {
    return;
  }

  const headers = values[0];
  const index = {};
  headers.forEach((header, i) => {
    index[String(header).trim()] = i;
  });

  const required = ['Status', 'To', 'Subject', 'Body', 'Sent At', 'Error'];
  for (const column of required) {
    if (!(column in index)) {
      throw new Error(`Missing Notification Queue column: ${column}`);
    }
  }

  for (let r = 1; r < values.length; r++) {
    const row = values[r];
    const status = String(row[index.Status] || '').trim();
    if (status !== 'Pending') {
      continue;
    }

    try {
      MailApp.sendEmail({
        to: String(row[index.To] || '').trim(),
        subject: String(row[index.Subject] || '').trim(),
        body: String(row[index.Body] || '').trim(),
      });
      sheet.getRange(r + 1, index.Status + 1).setValue('Sent');
      sheet.getRange(r + 1, index['Sent At'] + 1).setValue(new Date());
      sheet.getRange(r + 1, index.Error + 1).setValue('');
    } catch (error) {
      sheet.getRange(r + 1, index.Status + 1).setValue('Error');
      sheet.getRange(r + 1, index.Error + 1).setValue(String(error));
    }
  }
}
