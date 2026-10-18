import type { PlanNode } from "../api";
import { formatCount } from "../format";

export function PlanView({ plan }: { plan: PlanNode }) {
  const max = Math.max(1, ...collect(plan).map((n) => Math.max(n.rows ?? 0, n.est_rows ?? 0)));
  return (
    <div className="plan" role="tree" aria-label="Query plan">
      <PlanRow node={plan} max={max} depth={0} />
    </div>
  );
}

function PlanRow({ node, max, depth }: { node: PlanNode; max: number; depth: number }) {
  const actual = node.rows;
  const estimate = node.est_rows;
  const scale = (n: number) => `${(Math.log10(n + 1) / Math.log10(max + 1)) * 100}%`;
  return (
    <div className="plan-node" role="treeitem" aria-expanded={node.children.length > 0 || undefined}>
      <div className="plan-line" style={{ paddingLeft: depth * 16 }}>
        <span className={`op op-${node.operator}`}>{node.label}</span>
        <span className="plan-stats">
          {actual !== undefined && <span title="rows produced">{formatCount(actual)} rows</span>}
          {estimate !== null && <span className="muted" title="optimizer's estimate">est {formatCount(estimate)}</span>}
          {node.ms !== undefined && <span className="muted">{node.ms.toFixed(2)} ms</span>}
        </span>
      </div>
      <div className="plan-bars" style={{ marginLeft: depth * 16 }} aria-hidden="true">
        {estimate !== null && <i className="bar estimate" style={{ width: scale(estimate) }} />}
        {actual !== undefined && <i className="bar actual" style={{ width: scale(actual) }} />}
      </div>
      {node.subplans.map((sub, i) => (
        <div key={i}>
          <div className="plan-sub muted small" style={{ paddingLeft: (depth + 1) * 16 }}>
            subquery ({sub.kind}{sub.correlated ? ", runs once per row" : ", runs once"})
          </div>
          <PlanRow node={sub.plan} max={max} depth={depth + 2} />
        </div>
      ))}
      {node.children.map((child) => (
        <PlanRow key={child.id} node={child} max={max} depth={depth + 1} />
      ))}
    </div>
  );
}

function collect(node: PlanNode): PlanNode[] {
  return [node, ...node.children.flatMap(collect), ...node.subplans.flatMap((s) => collect(s.plan))];
}
