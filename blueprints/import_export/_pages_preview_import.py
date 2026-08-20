"""pages — split from import_export.py."""
from ._common import *  # noqa

@import_export_bp.route('/preview', methods=['POST'])
@login_required
def preview_import():
    """Analyze file and return preview data."""
    file = request.files.get('file')
    if not file:
        return jsonify({'error': 'No file uploaded'}), 400
    
    try:
        if file.filename.endswith('.csv'):
            df = pd.read_csv(file)
        else:
            df = pd.read_excel(file)
            
        # Normalize columns
        df.columns = [c.lower().strip().replace(' ', '_') for c in df.columns]
        
        # Detect Dataset Type
        dataset_type = 'unknown'
        if 'qty' in df.columns and 'item' in df.columns:
            dataset_type = 'dispatch'
        elif 'amount' in df.columns and 'reason' in df.columns:
            dataset_type = 'pending_bills'
        elif 'phone' in df.columns and 'address' in df.columns:
            dataset_type = 'clients'
            
        preview_data = df.head(10).fillna('').to_dict(orient='records')
        
        return jsonify({
            'success': True,
            'dataset_type': dataset_type,
            'columns': list(df.columns),
            'row_count': len(df),
            'preview': preview_data
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

