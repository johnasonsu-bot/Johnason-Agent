import type { RendererProps } from "./renderers";

export const TableRenderer = ({ artifact }: RendererProps) => {
  const rows = Array.isArray(artifact.data) ? artifact.data as Record<string, unknown>[] : [];
  const columns = rows[0] ? Object.keys(rows[0]) : [];
  return (
    <table data-testid={`artifact-${artifact.id}`}>
      <caption>{artifact.title}</caption>
      <thead><tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr></thead>
      <tbody>{rows.map((row, index) => <tr key={index}>{columns.map((column) => <td key={column}>{String(row[column])}</td>)}</tr>)}</tbody>
    </table>
  );
};
