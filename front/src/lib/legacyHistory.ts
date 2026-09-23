/**
 * 旧版本把重新登录 / SSO 检查历史存在浏览器 IndexedDB 里。
 * 现在历史统一放服务端；这里只负责把旧数据读出来交给服务端，然后删掉本地库。
 */

export function readLegacyHistory<T>(dbName: string, storeName: string): Promise<T[]> {
  if (typeof indexedDB === "undefined") return Promise.resolve([]);
  return new Promise<T[]>((resolve) => {
    let request: IDBOpenDBRequest;
    try {
      request = indexedDB.open(dbName);
    } catch {
      resolve([]);
      return;
    }
    // 库不存在时 open 会新建一个空库；不创建对象仓库，后面按空处理并删掉。
    request.onupgradeneeded = () => undefined;
    request.onerror = () => resolve([]);
    request.onblocked = () => resolve([]);
    request.onsuccess = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(storeName)) {
        db.close();
        resolve([]);
        return;
      }
      try {
        const query = db.transaction(storeName, "readonly").objectStore(storeName).getAll();
        query.onsuccess = () => {
          const rows = Array.isArray(query.result) ? (query.result as T[]) : [];
          db.close();
          resolve(rows);
        };
        query.onerror = () => {
          db.close();
          resolve([]);
        };
      } catch {
        db.close();
        resolve([]);
      }
    };
  });
}

export function dropLegacyHistory(dbName: string) {
  if (typeof indexedDB === "undefined") return;
  try {
    indexedDB.deleteDatabase(dbName);
  } catch {
    // 删不掉也无妨，下次打开历史页会再试一次。
  }
}
