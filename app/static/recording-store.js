/* Local recovery store. Isolation is by account/project, not encryption at rest.
 * A shared browser profile/device can inspect IndexedDB: the UI discloses this.
 * Only committed MediaRecorder chunks survive refresh; a crash may lose the tail.
 */
'use strict';
(() => {
  const DB_NAME = 'shiguang-recording-recovery';
  let opening;
  function open() {
    if (!window.indexedDB) return Promise.reject(new Error('浏览器不支持本地录音恢复，请改用录音设备后上传。'));
    if (!opening) opening = new Promise((resolve, reject) => {
      const request = indexedDB.open(DB_NAME, 1);
      request.onupgradeneeded = () => {
        const db = request.result;
        const recordings = db.createObjectStore('recordings', {keyPath: 'id'});
        recordings.createIndex('userId', 'userId');
        const chunks = db.createObjectStore('chunks', {keyPath: ['recordingId', 'sequence']});
        chunks.createIndex('recordingId', 'recordingId');
      };
      request.onerror = () => { opening = null; reject(new Error('无法打开本地录音恢复空间，请检查浏览器隐私设置或可用空间。')); };
      request.onblocked = () => { opening = null; reject(new Error('其他标签页占用了本地录音存储，请关闭其他拾光页面后重试。')); };
      request.onsuccess = () => {
        const db = request.result;
        db.onversionchange = () => { db.close(); opening = null; };
        resolve(db);
      };
    });
    return opening;
  }
  function transaction(db, mode, callback) {
    return new Promise((resolve, reject) => {
      const tx = db.transaction(['recordings', 'chunks'], mode);
      let result;
      tx.oncomplete = () => resolve(result);
      tx.onerror = tx.onabort = () => reject(new Error('录音保存失败，可能是空间不足或浏览器限制。请先下载已录内容。'));
      try { callback(tx.objectStore('recordings'), tx.objectStore('chunks'), value => { result = value; }, tx); }
      catch (error) { tx.abort(); reject(error); }
    });
  }
  function owned(row, userId, projectId) { return row && row.userId === userId && row.projectId === projectId; }
  function removeChunks(chunks, id) {
    const request = chunks.index('recordingId').openCursor(IDBKeyRange.only(id));
    request.onsuccess = () => { const cursor = request.result; if(cursor) { cursor.delete(); cursor.continue(); } };
  }
  async function create(userId, projectId, mime) {
    if (!userId || !projectId) throw new Error('请登录并打开一个故事后再录音。');
    const id = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const row = {id, userId, projectId, mime, createdAt: new Date().toISOString(), status: 'recording', chunks: 0, bytes: 0};
    return transaction(await open(), 'readwrite', (records, chunks, done) => { records.add(row); done(row); });
  }
  async function append(userId, projectId, id, sequence, blob) {
    if (!(blob instanceof Blob) || !blob.size) return;
    return transaction(await open(), 'readwrite', (records, chunks, done, tx) => {
      const request = records.get(id);
      request.onsuccess = () => {
        const row = request.result;
        if (!owned(row, userId, projectId)) { tx.abort(); return; }
        chunks.add({recordingId: id, sequence, blob});
        row.chunks++; row.bytes += blob.size; row.updatedAt = new Date().toISOString();
        records.put(row); done(row);
      };
    });
  }
  async function update(userId, projectId, id, values) {
    return transaction(await open(), 'readwrite', (records, chunks, done, tx) => {
      const request = records.get(id);
      request.onsuccess = () => {
        const row = request.result;
        if (!owned(row, userId, projectId)) { tx.abort(); return; }
        for(const key of ['status', 'note', 'uploadedSourceId']) if(key in values) row[key] = values[key];
        row.updatedAt = new Date().toISOString(); records.put(row); done(row);
      };
    });
  }
  async function list(userId, projectId) {
    return transaction(await open(), 'readonly', (records, chunks, done) => {
      const request = records.index('userId').getAll(IDBKeyRange.only(userId));
      request.onsuccess = () => done(request.result.filter(row => !projectId || row.projectId === projectId).sort((a,b)=>b.createdAt.localeCompare(a.createdAt)));
    });
  }
  async function read(userId, projectId, id) {
    return transaction(await open(), 'readonly', (records, chunks, done, tx) => {
      const request = records.get(id);
      request.onsuccess = () => {
        const row = request.result;
        if (!owned(row, userId, projectId)) { tx.abort(); return; }
        const parts = chunks.index('recordingId').getAll(IDBKeyRange.only(id));
        parts.onsuccess = () => done({...row, blob: new Blob(parts.result.sort((a,b)=>a.sequence-b.sequence).map(part=>part.blob), {type: row.mime})});
      };
    });
  }
  async function remove(userId, projectId, id) {
    return transaction(await open(), 'readwrite', (records, chunks, done, tx) => {
      const request = records.get(id);
      request.onsuccess = () => {
        if (!request.result) { done(); return; }
        if (!owned(request.result, userId, projectId)) { tx.abort(); return; }
        records.delete(id); removeChunks(chunks,id); done();
      };
    });
  }
  async function clearUser(userId) {
    return transaction(await open(), 'readwrite', (records, chunks, done) => {
      const request = records.index('userId').getAll(IDBKeyRange.only(userId));
      request.onsuccess = () => { for(const row of request.result) { records.delete(row.id); removeChunks(chunks,row.id); } done(request.result.length); };
    });
  }
  window.RecordingStore = Object.freeze({create, append, update, list, read, remove, clearUser});
})();
