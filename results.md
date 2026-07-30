## Results
### Setup
```
    stages: [
      // Let HPA reach minimum replicas
      { duration: '5m', target: 50 },
      // Sudden burst
      { duration: '30s', target: 1500 },
      // Give HPA time to react, but observe damage
      { duration: '5m', target: 1500 },
      // Traffic disappears
      // Keep it low longer than stabilization window
      { duration: '5m', target: 100 },
      // Second burst after HPA finally scaled down
      { duration: '30s', target: 1000 },
      // Observe recovery
      { duration: '4m', target: 1000 },
    ]

```

### no HPA
```

╰─ k6 run --log-output=none k6s-load-expanded.js

         /\      Grafana   /‾‾/
    /\  /  \     |\  __   /  /
   /  \/    \    | |/ /  /   ‾‾\
  /          \   |   (  |  (‾)  |
 / __________ \  |_|\_\  \_____/


     execution: local
        script: k6s-load-expanded.js
        output: -

     scenarios: (100.00%) 1 scenario, 1500 max VUs, 20m30s max duration (incl. graceful stop):
              * hpa_attack_pattern: Up to 1500 looping VUs for 20m0s over 6 stages (gracefulRampDown: 30s, gracefulStop: 30s)



  █ THRESHOLDS

    http_req_duration
    ✓ 'p(95)<5000' p(95)=0s
    ✓ 'p(99)<10000' p(99)=5.37s

    http_req_failed
    ✗ 'rate<0.05' rate=98.29%


  █ TOTAL RESULTS

    checks_total.......: 1434677 1192.768886/s
    checks_succeeded...: 1.14%   16450 out of 1434677
    checks_failed......: 98.85%  1418227 out of 1434677

    ✗ homepage
      ↳  1% — ✓ 4342 / ✗ 354359
    ✗ product page
      ↳  1% — ✓ 4562 / ✗ 354125
    ✗ cart add
      ↳  1% — ✓ 4292 / ✗ 354365
    ✗ checkout
      ↳  0% — ✓ 3254 / ✗ 355378

    HTTP
    http_req_duration..............: avg=189.67ms min=0s       med=0s       max=43.25s p(90)=0s    p(95)=0s
      { expected_response:true }...: avg=4.14s    min=133.63ms med=652.83ms max=33.68s p(90)=13.5s p(95)=17.97s
    http_req_failed................: 98.29%  1774066 out of 1804765
    http_reqs......................: 1804765 1500.454484/s

    EXECUTION
    iteration_duration.............: avg=2.73s    min=683.58ms med=1.83s    max=1m6s   p(90)=2.66s p(95)=2.78s
    iterations.....................: 358628  298.157927/s
    vus............................: 108     min=0                  max=1500
    vus_max........................: 1500    min=1500               max=1500

    NETWORK
    data_received..................: 237 MB  197 kB/s
    data_sent......................: 8.4 MB  7.0 kB/s




running (20m02.8s), 0000/1500 VUs, 358628 complete and 93 interrupted iterations
hpa_attack_pattern ✓ [======================================] 0000/1500 VUs  20m0s


```

### HPA configured
```

─ k6 run --log-output=none k6s-load-expanded.js

         /\      Grafana   /‾‾/
    /\  /  \     |\  __   /  /
   /  \/    \    | |/ /  /   ‾‾\
  /          \   |   (  |  (‾)  |
 / __________ \  |_|\_\  \_____/


     execution: local
        script: k6s-load-expanded.js
        output: -

     scenarios: (100.00%) 1 scenario, 1500 max VUs, 20m30s max duration (incl. graceful stop):
              * hpa_attack_pattern: Up to 1500 looping VUs for 20m0s over 6 stages (gracefulRampDown: 30s, gracefulStop: 30s)



  █ THRESHOLDS

    http_req_duration
    ✗ 'p(95)<5000' p(95)=8.14s
    ✗ 'p(99)<10000' p(99)=15.63s

    http_req_failed
    ✗ 'rate<0.05' rate=7.22%


  █ TOTAL RESULTS

    checks_total.......: 238073 196.561357/s
    checks_succeeded...: 90.18% 214716 out of 238073
    checks_failed......: 9.81%  23357 out of 238073

    ✗ homepage
      ↳  87% — ✓ 52366 / ✗ 7157
    ✗ product page
      ↳  90% — ✓ 54155 / ✗ 5368
    ✗ cart add
      ↳  90% — ✓ 53931 / ✗ 5592
    ✗ checkout
      ↳  91% — ✓ 54264 / ✗ 5240

    HTTP
    http_req_duration..............: avg=2.26s  min=0s       med=1s     max=1m0s  p(90)=5.87s  p(95)=8.14s
      { expected_response:true }...: avg=2.25s  min=132.79ms med=1.05s  max=59.6s p(90)=5.85s  p(95)=7.96s
    http_req_failed................: 7.22%  29728 out of 411573
    http_reqs......................: 411573 339.808996/s

    EXECUTION
    iteration_duration.............: avg=16.68s min=850.99ms med=12.68s max=1m22s p(90)=36.14s p(95)=43.47s
    iterations.....................: 59503  49.127748/s
    vus............................: 4      min=0               max=1500
    vus_max........................: 1500   min=1500            max=1500

    NETWORK
    data_received..................: 3.1 GB 2.5 MB/s
    data_sent......................: 90 MB  74 kB/s




running (20m11.2s), 0000/1500 VUs, 59503 complete and 20 interrupted iterations
hpa_attack_pattern ✓ [======================================] 0000/1500 VUs  20m0s

```

### HPA & AI predictions
```


╰─ k6 run --log-output=none k6s-load-expanded.js

         /\      Grafana   /‾‾/
    /\  /  \     |\  __   /  /
   /  \/    \    | |/ /  /   ‾‾\
  /          \   |   (  |  (‾)  |
 / __________ \  |_|\_\  \_____/


     execution: local
        script: k6s-load-expanded.js
        output: -

     scenarios: (100.00%) 1 scenario, 1500 max VUs, 20m30s max duration (incl. graceful stop):
              * hpa_attack_pattern: Up to 1500 looping VUs for 20m0s over 6 stages (gracefulRampDown: 30s, gracefulStop: 30s)



  █ THRESHOLDS

    http_req_duration
    ✓ 'p(95)<5000' p(95)=3.07s
    ✓ 'p(99)<10000' p(99)=4.52s

    http_req_failed
    ✓ 'rate<0.05' rate=0.00%


  █ TOTAL RESULTS

    checks_total.......: 513640 424.631783/s
    checks_succeeded...: 99.99% 513602 out of 513640
    checks_failed......: 0.00%  38 out of 513640

    ✓ homepage
    ✓ product page
    ✗ cart add
      ↳  99% — ✓ 128391 / ✗ 19
    ✗ checkout
      ↳  99% — ✓ 128391 / ✗ 19

    HTTP
    http_req_duration..............: avg=952.55ms min=130.98ms med=626.45ms max=9.67s  p(90)=2.3s   p(95)=3.07s
      { expected_response:true }...: avg=952.56ms min=133.12ms med=626.51ms max=9.67s  p(90)=2.3s   p(95)=3.07s
    http_req_failed................: 0.00%  41 out of 898855
    http_reqs......................: 898855 743.09322/s

    EXECUTION
    iteration_duration.............: avg=7.67s    min=1.17s    med=6.9s     max=27.26s p(90)=12.51s p(95)=14.71s
    iterations.....................: 128410 106.157946/s
    vus............................: 3      min=0            max=1500
    vus_max........................: 1500   min=1500         max=1500

    NETWORK
    data_received..................: 7.0 GB 5.8 MB/s
    data_sent......................: 196 MB 162 kB/s




running (20m09.6s), 0000/1500 VUs, 128410 complete and 0 interrupted iterations
hpa_attack_pattern ✓ [======================================] 0000/1500 VUs  20m0s

```