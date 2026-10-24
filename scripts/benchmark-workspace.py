"""Measure real private-worker calls on a disposable 10,000-task workspace.

Fixture insertion is deliberately outside the timing. No user data is opened.
The report separates observed results, interactive targets and CI regression
ceilings; passing a ceiling is not a claim that the target has been achieved.
"""
import argparse
from datetime import date, timedelta
import json
import math
import os
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time


def measure(operation, count):
    values=[];size=0;result=None
    for _ in range(count):
        start=time.perf_counter()
        result=operation()
        encoded=json.dumps(result,ensure_ascii=False).encode('utf-8')
        values.append((time.perf_counter()-start)*1000)
        size=max(size,len(encoded))
    ordered=sorted(values)
    return dict(samples=count,p50_ms=round(statistics.median(values),2),
                p95_ms=round(ordered[max(0,math.ceil(count*.95)-1)],2),
                max_ms=round(max(values),2),response_bytes=size),result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tasks',type=int,default=10000)
    parser.add_argument('--samples',type=int,default=7)
    parser.add_argument('--light',action='store_true',help='Quick baseline: sparse checklists and no attachment files')
    parser.add_argument('--report',type=Path,default=Path('artifacts/native-performance.json'))
    parser.add_argument('--max-query-ms',type=float,default=2000)
    parser.add_argument('--max-save-ms',type=float,default=3000)
    args=parser.parse_args()
    if args.tasks<10000 or args.samples<3:parser.error('At least 10,000 tasks and 3 samples are required')
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'source'/'taskboard'))
    with tempfile.TemporaryDirectory(prefix='taskboard-performance-') as temporary:
        os.environ['TASKBOARD_DATA_ROOT']=temporary
        import native_worker
        import pro_features
        worker=native_worker.Worker()
        try:
            worker.dispatch('login',dict(password='isolated-performance-only',confirm='isolated-performance-only'))
            stamp='2026-01-01T09:00:00.000000';due=(date.today()+timedelta(days=3)).isoformat()
            records=[(f'性能目标{i:05d}','用于模拟长期工作区的中文任务正文。'*4,
                      ['计划中','进行中','完成'][i%3],due,stamp,stamp,i*1000) for i in range(args.tasks)]
            with worker.db:
                worker.db.executemany('INSERT INTO tasks(title,body,status,due_date,created_at,updated_at,sort_order) VALUES(?,?,?,?,?,?,?)',records)
                checks=[(i+1,f'检查步骤{n}',n==0,n) for i in range(0,args.tasks,5 if args.light else 1) for n in range(3 if args.light else 5)]
                worker.db.executemany('INSERT INTO checklist_items(task_id,label,done,sort_order) VALUES(?,?,?,?)',checks)
                if not args.light:
                    files=Path(temporary)/'attachments'
                    attachments=[]
                    for i in range(args.tasks):
                        stored=f'{i:032x}.txt'
                        (files/stored).write_text(f'Synthetic attachment {i}',encoding='utf-8')
                        attachments.append((i+1,f'说明文件{i:05d}.txt',stored,0,stamp))
                    worker.db.executemany('INSERT INTO attachments(task_id,filename,stored_name,is_image,created_at) VALUES(?,?,?,?,?)',attachments)
            pro_features.migrate(worker.db)
            # Warm schema/index initialization before measuring repeated requests.
            worker.dispatch('pro-query',dict(limit=100))
            search,result=measure(lambda:worker.dispatch('pro-query',dict(query=f'性能目标{args.tasks-1:05d}',scope='active',limit=100)),args.samples)
            assert result['total']==1 and len(result['tasks'])==1,result
            listing,result=measure(lambda:worker.dispatch('pro-query',dict(scope='active',limit=100,sort='due')),args.samples)
            assert result['total']==args.tasks and len(result['tasks'])==100,(result['total'],len(result['tasks']))
            snapshot,result=measure(lambda:worker.dispatch('state',{}),3)
            assert len(result['tasks'])==args.tasks,len(result['tasks'])
            counter=0
            def save():
                nonlocal counter
                counter+=1
                token=worker.db.execute('SELECT updated_at FROM tasks WHERE id=?',(args.tasks,)).fetchone()[0]
                return worker.dispatch('save',dict(id=args.tasks,updated_at=token,title=f'性能保存{counter}',note='计时真实保存及返回状态',status='计划中',due=due,tags=[],checklist=[],options=dict(priority=2)))
            saving,_=measure(save,args.samples)
            observations=dict(search=search,page_100=listing,full_state=snapshot,save=saving)
            ceilings=dict(search=args.max_query_ms,page_100=args.max_query_ms,save=args.max_save_ms)
            failures=[name for name,limit in ceilings.items() if observations[name]['p95_ms']>limit]
            report=dict(version=native_worker.VERSION,python=platform.python_version(),platform=platform.platform(),
                        dataset=dict(tasks=args.tasks,checklist_items=len(checks),attachments=0 if args.light else args.tasks,synthetic=True),
                        timings_include='Worker.dispatch plus UTF-8 JSON serialization; fixture generation excluded',
                        observations=observations,interactive_targets_ms=dict(search=150,page_100=250,save=300),
                        ci_regression_ceilings_ms=ceilings,ceiling_failures=failures,passed=not failures)
            args.report.parent.mkdir(parents=True,exist_ok=True)
            args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(report,ensure_ascii=False,indent=2))
            return 1 if failures else 0
        finally:
            worker.close()


if __name__=='__main__':raise SystemExit(main())
