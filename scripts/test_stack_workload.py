"""Compare Python/native construction and Python/C++ scheduling on a small stack."""
import argparse
import json
import os
import sys
from pathlib import Path

OMELET = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(OMELET.parents[1]), str(OMELET)]
from chip.comm.online_workload_builder import OnlineWorkloadBuilder, _write_bz2_group
from chip.comm.omelet_mapping import OmeletNodeMap
from chip.comm.omelet_scheduler import run_omelet_workload
from omelet.backends.gem5.model_config import write_model_configs
from omelet.backends.gem5.adapter import build_gem5_cmd
from omelet.backends.gem5.online import OnlineGem5Session


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--layers', type=int, default=2)
    parser.add_argument('--reuse', action='store_true')
    parser.add_argument('--bottom-grid', type=int, default=2)
    parser.add_argument('--top-grid', type=int, default=2)
    parser.add_argument('--vertical-interconnect-method', type=int, choices=(0,1), default=0)
    args=parser.parse_args()
    out=args.output_dir.resolve(); out.mkdir(parents=True,exist_ok=True)
    if args.layers < 2 or min(args.bottom_grid,args.top_grid) <= 0:
        parser.error('layers must be >=2 and grid sizes positive')
    count = 0
    chips = {}
    pitch = max(args.bottom_grid,args.top_grid)
    for i in range(args.layers*2):
        size = args.bottom_grid if i < 2 else args.top_grid
        chips[str(i)] = {'chiplet_pos':[i%2,0,i//2],'origin':[i%2*pitch,0,i//2],
                         'grid':[size,size],'node_id_base':count}
        count += size*size
    grid=OnlineWorkloadBuilder()._write_nonuniform_tier_grid_config(str(out),chiplets=chips)
    configs=write_model_configs(out/'config',k_chip=2,k_node=2,nonuniform_grid_file=grid,
                                on_chip_width_bits=384,off_chip_serial_width_bits=2688,
                                network_options={'vertical_interconnect_method':args.vertical_interconnect_method})
    positions=out/'trace_node_position_mapping.txt'
    rows=[]; node_chip={}
    for key,meta in chips.items():
        size = meta['grid'][0]
        for local in range(size*size):
            node = meta['node_id_base']+local
            node_chip[node]=int(key)
            rows.append(f"{node} {meta['origin'][0]+local%size} {local//size} {meta['chiplet_pos'][2]} {key} {key} real {node}")
    positions.write_text('\n'.join(rows)+'\n',encoding='utf-8')
    top=count-2*args.top_grid**2
    endpoints=[(top,count-1),(count-1,top),(0,top),(top,0)]
    if args.reuse:
        endpoints=[(top,count-1),(count-1,0),(0,args.bottom_grid**2),(args.bottom_grid**2,top)]
    for i,(src,dst) in enumerate(endpoints):
        _write_bz2_group(str(out/f'group_{i}.bz2'),[{'src':100 if args.reuse else src,
            'dst':101 if args.reuse else dst,'size_bytes':8192,'shared_key':-1}],num_nodes=102 if args.reuse else count)
    manifest={'num_inputs':1,'num_resources':count,
        'template_groups':[{'group_id':i,'bz2_path':f'group_{i}.bz2'} for i in range(len(endpoints))],
        'phases':[{'phase_id':i,'input_id':0,'layer_id':i,'compute_latency_cycles':5,
                   'dep_phase_ids':[],'resource_ids':[i],'group_id':i} for i in range(len(endpoints))]}
    if args.reuse:
        manifest['endpoint_mapping_catalog']='endpoint_mapping_catalog.json'
        catalog={'auto_reuse':1,'resolve_mode':1,
            'virtual_tiles':[{'virtual_id':100+i,'physical_id':p,'layer_id':i} for i,(p,_) in enumerate(endpoints)],
            'physical_nodes':[{'physical_id':n,'tier_id':node_chip[n]} for n in range(count)],
            'layers':[{'layer_id':i,'block_id':i,'virtual_ids':[100+i], 'canonical':[endpoint],
                       'candidates':[[endpoint]]} for i,(endpoint,_) in enumerate(endpoints)]}
        # Virtual destinations belong to the next layer; test the first two groups.
        for i in range(len(endpoints)):
            _write_bz2_group(str(out/f'group_{i}.bz2'),[{'src':100+i,'dst':100+(i+1)%len(endpoints),
                'size_bytes':8192,'shared_key':-1}],num_nodes=100+len(endpoints))
        (out/'endpoint_mapping_catalog.json').write_text(json.dumps(catalog))
    manifest_file=out/'manifest.json'; manifest_file.write_text(json.dumps(manifest))
    mapping=OmeletNodeMap(positions,k_chip=2,k_node=2,nonuniform_grid_path=grid)
    results=[]
    for native,engine in ((0,'python'),(1,'python'),(1,'cpp')):
        run=out/f'native{native}_{engine}'
        command=build_gem5_cmd(topology='irregular',material='org',bonding='ubump30',
            injection_rate=0,sim_cycles=100000,synthetic='uniform_random',outdir=str(run),num_cpus=count,
            sys_config=str(configs['system']),net_config=str(configs['network']),noc_config=str(configs['noc']),
            noi_config=str(configs['noi']),plc_config=str(configs['placement']),nonuniform_grid_config=str(grid))
        command+=['--buffers-per-ctrl-vc=16','--buffers-per-data-vc=16','--xor-low-bit=0','--c2c_bonding=ubump10']
        os.environ['OMELET_NATIVE_GRAPH']=str(native)
        with OnlineGem5Session(command,run) as session:
            if engine=='python':
                result=run_omelet_workload(manifest_file,mapping,session,1.0,endpoint_mode='physical')
            else:
                response=session.run_cpp(manifest=str(manifest_file),position=str(positions),
                    catalog=str(out/'endpoint_mapping_catalog.json') if args.reuse else '',nonuniform_grid=str(grid),
                    endpoint_mode='physical',k_node=2,k_chip=2,frequency_ghz=1.0,result_path=str(run/'result.json'))
                result=json.loads(Path(response['result_path']).read_text())
        assert result['message_arrived']==len(endpoints), result
        results.append(result)
        (run/'snapshot.json').write_text(json.dumps(result,indent=2))
    for result in results[1:]:
        for key in ('final_cycle','phases','groups','inputs','message_arrived'):
            expected = results[0][key]
            actual = result[key]
            if key == 'groups':
                # Python alone records template filenames; compare all timing/count fields.
                expected = [{k:v for k,v in row.items() if k != 'template_bz2'} for row in expected]
                actual = [{k:v for k,v in row.items() if k != 'template_bz2'} for row in actual]
            if key == 'phases':
                for row in expected:
                    if 'runtime_chiplets' in row:
                        assert row['runtime_chiplets'] == sorted({node_chip[p] for p in row['resource_ids']})
                expected = [{k:v for k,v in row.items() if k != 'runtime_chiplets'} for row in expected]
                actual = [{k:v for k,v in row.items() if k != 'runtime_chiplets'} for row in actual]
            assert actual==expected,(key,actual,expected)
    print(f'Stack verified: layers={args.layers}, grids={args.bottom_grid}/{args.top_grid}, reuse={args.reuse}, packets={len(endpoints)}; construction and scheduler timing identical')


if __name__=='__main__':
    main()
