// FIXTURE (not app code): proves the TITAN-FRT-014 no-fetch rule fires. A direct
// fetch outside packages/api-client and the BFF must be a lint error.
export async function loadTasks(): Promise<unknown> {
  const response = await fetch("/api/v1/tasks");
  return response.json();
}
