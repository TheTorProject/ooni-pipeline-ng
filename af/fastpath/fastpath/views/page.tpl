<!DOCTYPE html>
<html lang="en">
  <head>
    <title>{{title}}</title>
    <style>
  body svg {
    width: 99%;
    height: 40em;
  }
  form {
    width: 100%;
    margin: 0 auto;
  }

  label, input {
      display: inline-block;
  }

  label {
      width: 30%;
      text-align: right;
  }

  label + input {
      width: 30%;
      margin: 0 30% 0 4%;
  }

  input + input {
      float: right;
  }

  svg g#msmts circle {
    border: 1px solid #888;
    stroke: #606060;
    stroke-width: .5px;
    opacity: 0.2;
  }
  line.change {
    stroke-width: 4px;
    opacity: 0.2;
  }
    </style>
  </head>
  <body>
    <form action="/chart" method="get">
        <label for="name">CCs: </label>
        <input type="text" name="ccs" value="{{form['ccs']}}" required>

        <label for="name">test names: </label>
        <input type="text" name="test_names" value="{{form['test_names']}}" required>

        <label for="name">inputs: </label>
        <input type="text" name="inputs" value="{{form['inputs']}}">

        <label for="name">start date: </label>
        <input type="text" name="start_date" value="{{form['start_date']}}">

        <label for="name">ASNs: </label>
        <input type="checkbox" name="split_asn" value="{{form['split_asn']}}">
      <input type="submit" />
    </form>
    % for c in charts:
    %   msmts = c["msmts"]
    %   changes = c["changes"]
    %   x_scale = c["x_scale"]
    %   start_d = c["start_d"]
    %   end_d = c["end_d"]
    %   x1 = c["x1"]
    %   x2 = c["x2"]
    %   y1 = c["y1"]
    %   y2 = c["y2"]
    %   title = c["title"]
      <svg viewBox="0 0 {{x2 + 100}} {{y2}}" version="1.1" xmlns:xlink="http://www.w3.org/1999/xlink" xmlns="http://www.w3.org/2000/svg">
        <style>
      .txt { font: 13px sans-serif }
      .dot {
        stroke-opacity: .2;
      }
        body svg {
          width: 99%;
          height: 40em;
        }
        svg g#msmts circle {
          border: 1px solid #888;
          stroke: #606060;
          stroke-width: .5px;
          opacity: 0.1;
        }
        line.change {
          stroke-width: 4px;
          opacity: 0.2;
        }
        </style>
        <text x="{{x1+100}}" y="20" class="txt">{{title}}</text>
        <g id="msmts">
        % pcx = pcy = None
        % for d, val, mean in msmts:
        % cx = (d - start_d).total_seconds() * x_scale + x1
        % cy = y2 - min(max(val, 0) * 200, 300)
        % #r = "{:02x}".format(min(int(max(val, 0) * 170), 255))
        % col = "d60000" if val > .5 else "00d600"
        <circle class="dot" style="fill:#{{col}};" cx="{{cx}}" cy="{{cy}}"
                                                                r="4"></circle>

        % # moving average
        % cy = y2 - min(max(mean, 0) * 200, 300)
        % if pcy is not None:
        <line x1="{{pcx}}" x2="{{cx}}" y1="{{pcy}}" y2="{{cy}}" stroke-width="2" stroke="#d2eaff"></line>
        % end
        % pcx, pcy = cx, cy
        % end

        % # changes in blocking
        % for c in changes:
        %   cx = (c.measurement_start_time - start_d).total_seconds() * x_scale + x1
        %   col = "ff3333" if c.blocked else "33ff33"
        <line class="change" x1="{{cx}}" x2="{{cx}}" y1="{{y1 + 50}}" y2="{{y2}}" stroke="#{{col}}"></line>
        % end

        </g>
        % # start/end date labels
        <text x="{{x1-80}}" y="{{y2+30}}" class="txt">{{start_d}}</text>
        <text x="{{x2-80}}" y="{{y2+30}}" class="txt">{{end_d}}</text>

        <line x1="{{x1}}" x2="{{x2}}" y1="{{y2}}" y2="{{y2}}" stroke="#888"></line>
        <line x1="{{x1}}" x2="{{x1}}" y1="{{y1}}" y2="{{y2}}" stroke="#888"></line>

        % for val in (0.0, 1.0, 2.0):
        % cy = y2 - min(max(val, 0) * 100, (y2 - y1))
        <line x1="{{x1-5}}" x2="{{x1-10}}" y1="{{cy}}" y2="{{cy}}" stroke="#888"></line>
        % end
      </svg>
      % end
  </body>
</html>
