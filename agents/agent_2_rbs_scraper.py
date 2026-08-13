import requests
from bs4 import BeautifulSoup
import concurrent.futures
from core.cache import RBSCache
from config import RBS_URL

import warnings
warnings.filterwarnings('ignore', message='Unverified HTTPS request')

class RBSScraper:
    def __init__(self):
        self.cache = RBSCache()
        self.session = requests.Session()
        # Government sites often have bad SSL configs, verify=False avoids crashes
        self.session.verify = False 

    def _fetch_single_route(self, source, destination):
        # Check cache first
        cached = self.cache.get_route(source, destination)
        if cached and cached['status'] == 'SUCCESS':
            return source, destination, cached['distance'], cached['route_sequence'], cached['junctions']
            
        data = {
            'srcCode': source,
            'destCode': destination,
            'gaugeType': 'S',
            'distance': 'goods',
            'PageName': 'ShortPath'
        }
        
        try:
            r = self.session.post(RBS_URL, data=data, timeout=30)
            soup = BeautifulSoup(r.text, 'html.parser')
            
            rows = soup.find_all('tr')
            route_sequence = []
            distance = 0.0
            
            for row in rows:
                cols = row.find_all('td')
                if len(cols) > 3:
                    # Station code is in the 2nd column
                    st_code = cols[1].text.strip().split('\n')[0].strip()
                    if st_code and len(st_code) <= 8 and ' ' not in st_code: 
                        # Cumulative Distance is usually in the 4th column
                        route_sequence.append(st_code)
                        d_text = cols[3].text.strip()
                        if d_text.replace('.', '', 1).isdigit():
                            distance = float(d_text)
                            
            if not route_sequence:
                # print(f"No route found for {source}->{destination}")
                self.cache.set_route(source, destination, 0, [], [], status="FAILED")
                return source, destination, None, [], []
                
            junctions = [code for code in route_sequence if "JN" in code]
            
            self.cache.set_route(source, destination, distance, route_sequence, junctions, status="SUCCESS")
            return source, destination, distance, route_sequence, junctions
            
        except Exception as e:
            # print(f"Error for {source}->{destination}: {e}")
            self.cache.set_route(source, destination, 0, [], [], status="FAILED")
            return source, destination, None, [], []

    def get_routes_batch(self, od_pairs):
        """
        Fetches multiple routes in parallel using ThreadPoolExecutor.
        od_pairs is a list of tuples: (source, destination).
        """
        if not od_pairs:
            return {}

        unique_od_pairs = list(set(od_pairs))
        results_dict = {}
        
        # 20 workers provides lightning-fast parallel execution without overwhelming the OS limit
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(self._fetch_single_route, src, dest) for src, dest in unique_od_pairs]
            
            for future in concurrent.futures.as_completed(futures):
                src, dest, dist, route, juncs = future.result()
                results_dict[(src, dest)] = (dist, route, juncs)
                
        return results_dict
