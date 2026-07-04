// FIXTURE: must FAIL the TITAN-FRT-010 token rule (raw hex + rgb()).
export function BadCard() {
  return <div className="bg-[#ff0000]" style={{ color: "rgb(255, 0, 0)" }} />;
}
