// The labs: experiments that run the same workload several ways and compare what each one costs.
import { useState } from "react";
import { local, readStorage, writeStorage } from "../storage";
import { EngineLab } from "./EngineLab";
import { PoolLab } from "./PoolLab";
import { RaftLab } from "./RaftLab";
import { ScalingLab } from "./ScalingLab";
import { AccessLab } from "./AccessLab";

const LABS = [
  { id: "engines", label: "B+tree vs LSM tree", Lab: EngineLab },
  { id: "buffer-pool", label: "Buffer pool policies", Lab: PoolLab },
  { id: "replication", label: "Replication (Raft)", Lab: RaftLab },
  { id: "access", label: "Access methods", Lab: AccessLab },
  { id: "scaling", label: "Scaling (TPC-H)", Lab: ScalingLab },
] as const;
type LabId = (typeof LABS)[number]["id"];
const LAB_KEY = "arcanum.lab";

export function Labs({ sessionId }: { sessionId: string | null }) {
  const [current, setCurrent] = useState<LabId>(() => LABS.find((l) => l.id === readStorage(local, LAB_KEY))?.id ?? "engines");
  const choose = (id: LabId) => {
    setCurrent(id);
    writeStorage(local, LAB_KEY, id);
  };
  return (
    <div className="labs">
      <div className="lab-tabs" role="tablist" aria-label="Labs">
        {LABS.map((l) => (
          <button key={l.id} role="tab" id={`lab-tab-${l.id}`} aria-selected={current === l.id} aria-controls={`lab-${l.id}`}
            className={current === l.id ? "on" : ""} onClick={() => choose(l.id)}>
            {l.label}
          </button>
        ))}
      </div>
      {/* Both stay mounted so each keeps its settings and last result while the other is shown. */}
      {LABS.map(({ id, Lab }) => (
        <div key={id} role="tabpanel" id={`lab-${id}`} aria-labelledby={`lab-tab-${id}`} hidden={current !== id}>
          <Lab sessionId={sessionId} />
        </div>
      ))}
    </div>
  );
}
