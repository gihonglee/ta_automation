function onNewFileUpload() {
    var folder = DriveApp.getFolderById("");  // your Drive folder ID
    var sheet = SpreadsheetApp.openById("").getSheetByName("Log"); // target spreadsheet
    var files = folder.getFiles();
    var loggedIds = sheet.getRange("B:B").getValues().flat();  // we track file IDs in column B
  
    while (files.hasNext()) {
      var file = files.next();
      var fileId = file.getId();
      var fileName = file.getName();
  
      if (loggedIds.indexOf(fileId) === -1) {
        // Extract index from file name (e.g., "167. David Kim.pdf" → "167")
        var match = fileName.match(/^(\d+)\.\s*/);
        var index = match ? match[1] : "";
  
        // Clean name: remove the ".pdf"
        var cleanName = fileName.replace(/\.pdf$/i, "");
  
        Logger.log("📄 New File Uploaded: " + fileName);
        Logger.log("Extracted Index: " + index);
        Logger.log("Cleaned Name: " + cleanName);
        Logger.log("Appended to sheet at: " + new Date());
  
        // 🔁 NEW: Trigger your Cloud Function
        var payload = {
          file_id: fileId
        };
  
        var options = {
          method: "post",
          contentType: "application/json",
          payload: JSON.stringify(payload)
        };
  
        try {
          var response = UrlFetchApp.fetch("", options);
          Logger.log("✅ Cloud Function response: " + response.getContentText());
  
          // Append row after successful trigger
          sheet.appendRow([index, fileId, cleanName, new Date()]);
        } catch (e) {
          Logger.log("❌ Failed to call Cloud Function: " + e.toString());
        }
      }
    }
  }
  