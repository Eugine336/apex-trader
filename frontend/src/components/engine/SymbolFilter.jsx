// Right-aligned symbol filter input matching the new frontend's dark theme.
export default function SymbolFilter({ value, onChange, placeholder = "Filter by symbol…" }) {
  return (
    <div className="flex items-center">
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="ml-auto w-48 rounded-md border border-gray-700 bg-gray-800 px-3 py-1.5 text-sm text-gray-100 outline-none placeholder:text-gray-500 focus:border-emerald-500"
      />
    </div>
  );
}
